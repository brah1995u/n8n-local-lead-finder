import uuid
from datetime import datetime,timezone
import httpx
import pytest
from psycopg.types.json import Jsonb
from app import db,pipeline
from app.services import Apify,Airtable,ServiceError


def test_repeated_discovery_deduplicates_and_keeps_branches():
    a={"title":"Pizza Luna","placeId":"ChIJone","phone":"622123456","address":"Street 1","categoryName":"Pizzeria"}
    with db.connect() as c:
        first=pipeline.ingest(c,a,"gandia")
        assert pipeline.ingest(c,a,"gandia")==first
        branch=pipeline.ingest(c,{**a,"placeId":"ChIJtwo","address":"Street 2"},"gandia")
        assert branch!=first
        assert c.execute("SELECT count(*) AS n FROM places").fetchone()["n"]==2


def test_legacy_fallback_upgrades_to_place_id_without_duplicate():
    a={"title":"Pizza Luna","phone":"622123456","address":"Street 1","categoryName":"Pizzeria"}
    with db.connect() as c:
        first=pipeline.ingest(c,a,"gandia")
        assert pipeline.ingest(c,{**a,"placeId":"ChIJone"},"gandia")==first


def test_no_new_paid_start_after_lost_response(monkeypatch):
    monkeypatch.setattr(pipeline,"refresh_crm",lambda:None)
    calls=[]
    class FakeApify:
        def budget(self):return {"available_usd":1.0}
        def minimum_cap(self,actor):return 0.5
        def start(self,*args):
            calls.append(args)
            raise ServiceError("ambiguous",ambiguous=True)
        def get(self,*args,**kwargs):return {"items":[]}
    monkeypatch.setattr(pipeline,"Apify",FakeApify)
    pipeline.create_job("gandia",pilot=True)
    with pytest.raises(ServiceError):pipeline.discovery()
    with pytest.raises(ServiceError):pipeline.discovery()
    assert len(calls)==1
    with db.connect() as c:
        assert c.execute("SELECT status FROM passes WHERE status='uncertain'").fetchone()


def test_budget_pause_does_not_mark_complete(monkeypatch):
    monkeypatch.setattr(pipeline,"refresh_crm",lambda:None)
    class Fake:
        def budget(self):return {"available_usd":0.0,"cycle":{"endAt":"2099-01-01T00:00:00Z"}}
        def minimum_cap(self,actor):return 0.5
    monkeypatch.setattr(pipeline,"Apify",Fake)
    jid=pipeline.create_job("gandia")
    assert pipeline.discovery()["status"]=="paused_budget"
    pipeline.maintenance()
    with db.connect() as c:
        assert c.execute("SELECT status FROM jobs WHERE id=%s",(jid,)).fetchone()["status"]=="paused"


def test_remaining_credit_below_actor_minimum_never_posts_a_run(monkeypatch):
    monkeypatch.setattr(pipeline,'refresh_crm',lambda:None)
    class Fake:
        def budget(self):return {'available_usd':0.1415,'cycle':{'endAt':'2099-01-01T00:00:00Z'}}
        def minimum_cap(self,actor):return 0.5
        def start(self,*args):raise AssertionError('Run below minimum must not be submitted')
    monkeypatch.setattr(pipeline,'Apify',Fake)
    jid=pipeline.create_job('gandia')
    assert pipeline.discovery()['status']=='paused_budget'
    with db.connect() as c:
        job=c.execute('SELECT status,reason FROM jobs WHERE id=%s',(jid,)).fetchone()
        assert job['status']=='paused' and '$0.50' in job['reason']
        assert not c.execute('SELECT 1 FROM passes WHERE attempts>0').fetchone()


def test_apify_minimum_uses_current_price_not_future_price(monkeypatch):
    monkeypatch.setattr('app.services.secrets',lambda:{'apify_token':'fixture'})
    api=Apify()
    api.get=lambda path:{'pricingInfos':[{'startedAt':'2020-01-01T00:00:00Z','minimalMaxTotalChargeUsd':0.1},{'startedAt':'2025-01-01T00:00:00Z','minimalMaxTotalChargeUsd':0.5},{'startedAt':'2099-01-01T00:00:00Z','minimalMaxTotalChargeUsd':2}]}
    assert api.minimum_cap('fixture')==0.5


def test_apify_free_plan_and_reserve(monkeypatch):
    monkeypatch.setattr("app.services.secrets",lambda:{"apify_token":"fixture"})
    api=Apify()
    api.get=lambda path:{"plan":{"tier":"FREE","monthlyUsageCreditsUsd":5}} if path=="users/me" else {"monthlyUsageCycle":{"startAt":"2026-10-01","endAt":"2026-11-01"},"current":{"monthlyUsageUsd":4.2}}
    assert api.budget()["available_usd"]==0.3
    api.get=lambda path:{"plan":{"tier":"STARTER","monthlyBasePriceUsd":19}} if path=="users/me" else {}
    with pytest.raises(ServiceError):api.budget()


def test_airtable_api_cap_and_request_count(monkeypatch):
    monkeypatch.setattr("app.services.secrets",lambda:{"airtable_token":"fixture"})
    monkeypatch.setattr("app.services.time.sleep",lambda _:None)
    from zoneinfo import ZoneInfo
    cycle=datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m")
    with db.connect() as c:c.execute("INSERT INTO budgets(service,cycle,calls) VALUES('airtable',%s,599)",(cycle,))
    api=Airtable()
    api.client=httpx.Client(base_url="https://api.airtable.com/v0/",transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"records":[]})))
    assert api.records()==[]
    with pytest.raises(ServiceError):api.records()


def test_prior_refusal_skips_paid_enrichment():
    jid=pipeline.create_job("gandia")
    raw={"title":"Pizza Luna","phone":"622123456","address":"Street 1","categoryName":"Pizzeria"}
    with db.connect() as c:
        uid=pipeline.ingest(c,raw,"gandia",jid)
        c.execute("INSERT INTO suppressions(key,reason,source) VALUES('tel:+34622123456','Not Interested','legacy')")
        place=c.execute("SELECT * FROM places WHERE id=%s",(uid,)).fetchone()
        pipeline.add_search(c,jid,place)
        assert c.execute("SELECT inspect_status FROM places WHERE id=%s",(uid,)).fetchone()["inspect_status"]=="done"
        assert not c.execute("SELECT 1 FROM passes WHERE kind='serp'").fetchone()


def test_disqualified_existing_lead_updates_research_without_sales_fields(monkeypatch):
    raw={'title':'Pizza Luna','categoryName':'Pizzeria','phone':'622123456','address':'Street 1'}
    with db.connect() as c:
        uid=pipeline.ingest(c,raw,'gandia')
        inspection={'qualification':{'eligible':False,'priority':'C','status':'Не підходить','reason':'Виявлений власний кошик'},'checked_at':'2026-10-28T10:00:00Z'}
        c.execute("UPDATE places SET inspect_status='done',inspection=%s,airtable_id='rec-existing',synced_hash='old' WHERE id=%s",(Jsonb(inspection),uid))
    writes=[]
    class Fake:
        def records(self):return [{'id':'rec-existing','fields':{'Cortex ID':str(uid),'Статус':'Передзвонити','Нотатки':'Передзвонити у п’ятницю'}}]
        def upsert(self,rows):
            writes.extend(rows)
            return {'records':[{'id':'rec-existing','fields':{'Cortex ID':str(uid),'Статус':'Передзвонити'}}]}
    monkeypatch.setattr(pipeline,'refresh_crm',lambda:None)
    monkeypatch.setattr(pipeline,'Airtable',Fake)
    assert pipeline.sync()['synced']==1
    fields=writes[0]['fields']
    assert fields['Пріоритет']=='C'
    assert not {'Статус','Нотатки','Наступний контакт'} & fields.keys()


def test_public_marketplace_list_queues_maps_gap_check(monkeypatch):
    jid=pipeline.create_job('gandia')
    with db.connect() as c:c.execute('INSERT INTO coverage_pages(job_id,url) VALUES(%s,%s)',(jid,'https://glovoapp.com/es/es/gandia/categories/comida_1'))
    async def page(url):return {'links':[{'url':'https://glovoapp.com/es/es/gandia/stores/pizza-luna-gandia','text':'Pizza Luna Cerrado 98%'}]}
    monkeypatch.setattr(pipeline,'fetch_page',page)
    assert pipeline.check_coverage()['candidates']==1
    with db.connect() as c:
        payload=c.execute("SELECT input FROM passes WHERE key LIKE 'maps:coverage:%%'").fetchone()['input']
    assert payload['searchStringsArray']==['Pizza Luna']
    assert payload['scrapePlaceDetailPage'] is True


@pytest.mark.parametrize('run_status',['RUNNING','TIMED-OUT'])
def test_partial_serp_batch_only_completes_matched_query_pairs(monkeypatch,run_status):
    monkeypatch.setattr(pipeline,'refresh_crm',lambda:None)
    jid=pipeline.create_job('gandia')
    with db.connect() as c:
        entries=[]
        for index in range(2):
            uid=pipeline.ingest(c,{'title':f'Pizza Luna {index}','categoryName':'Pizzeria','phone':'622123456','address':f'Street {index}'},'gandia',jid)
            payload={'queries':f'name-{index}\nphone-{index}','_place_id':str(uid)}
            pipeline.insert_pass(c,jid,f'serp:{index}','serp',payload)
            p=c.execute('SELECT id FROM passes WHERE job_id=%s AND key=%s',(jid,f'serp:{index}')).fetchone()
            entries.append({'place_id':str(uid),'pass_id':p['id'],'queries':payload['queries'].splitlines()})
        parent=entries[0]['pass_id']
        c.execute("UPDATE passes SET status='running',run_id='fixture-run',dataset_id='fixture-data',input=jsonb_set(input,'{_batch}',%s) WHERE id=%s",(Jsonb(entries),parent))
        c.execute("UPDATE passes SET status='batched' WHERE id=%s",(entries[1]['pass_id'],))
    class Fake:
        def get(self,path,**params):
            if path.startswith('actor-runs/'):return {'status':run_status,'usageTotalUsd':0.01}
            return [{'searchQuery':{'term':q},'organicResults':[]} for q in ['name-0','phone-0','name-1']]
    monkeypatch.setattr(pipeline,'Apify',Fake)
    pipeline.discovery()
    with db.connect() as c:
        first=c.execute('SELECT raw FROM places WHERE id=%s',(entries[0]['place_id'],)).fetchone()['raw']
        second=c.execute('SELECT raw FROM places WHERE id=%s',(entries[1]['place_id'],)).fetchone()['raw']
        states={p['id']:p['status'] for p in c.execute("SELECT id,status FROM passes WHERE kind='serp'").fetchall()}
    assert first['_serp_complete'] is True
    assert not second.get('_serp_complete',False)
    assert states[parent]==('running' if run_status=='RUNNING' else 'done')
    assert states[entries[1]['pass_id']]==('batched' if run_status=='RUNNING' else 'failed')


def test_continue_retries_failed_run_without_losing_charge_history():
    jid=pipeline.create_job('gandia')
    with db.connect() as c:
        c.execute("INSERT INTO coverage_pages(job_id,url,status) VALUES(%s,'https://glovoapp.com/example','failed')",(jid,))
        pipeline.insert_pass(c,jid,'serp:fixture','serp',{'queries':'one\ntwo\nother','_place_id':str(uuid.uuid4())})
        p=c.execute("SELECT * FROM passes WHERE key='serp:fixture'").fetchone()
        batch=[{'pass_id':p['id'],'place_id':p['input']['_place_id'],'queries':['one','two']}]
        c.execute("UPDATE passes SET status='failed',run_id='failed-run',cost_usd=.12,input=jsonb_set(input,'{_batch}',%s) WHERE id=%s",(Jsonb(batch),p['id']))
    pipeline.create_job('gandia','continue')
    with db.connect() as c:
        old=c.execute('SELECT * FROM passes WHERE id=%s',(p['id'],)).fetchone()
        retry=c.execute("SELECT * FROM passes WHERE key='serp:fixture:retry'").fetchone()
        assert c.execute('SELECT status FROM coverage_pages WHERE job_id=%s',(jid,)).fetchone()['status']=='pending'
    assert old['status']=='superseded' and old['run_id']=='failed-run' and float(old['cost_usd'])==.12
    assert retry['status']=='pending' and retry['run_id'] is None
    assert retry['input']['queries']=='one\ntwo' and '_batch' not in retry['input']


def test_new_maps_website_rechecks_existing_record_without_new_search():
    a={'title':'Pizza Luna','placeId':'ChIJone','phone':'622123456','address':'Street 1','categoryName':'Pizzeria','website':'','_maps_checked_at':'2026-10-28','_serp_complete':True}
    with db.connect() as c:
        uid=pipeline.ingest(c,a,'gandia')
        c.execute("UPDATE places SET inspect_status='done' WHERE id=%s",(uid,))
        pipeline.ingest(c,{**a,'website':'https://pizzaluna.es'},'gandia')
        row=c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone()
    assert row['inspect_status']=='pending' and row['raw']['_serp_complete'] is True


def test_maps_identity_change_queues_new_generation_and_retires_old_niche():
    jid=pipeline.create_job('gandia')
    a={'title':'Pizza Luna','placeId':'ChIJone','phone':'622123456','address':'Street 1','categoryName':'Pizzeria','_maps_checked_at':'2026-10-28'}
    with db.connect() as c:
        uid=pipeline.ingest(c,a,'gandia',jid)
        pipeline.add_search(c,jid,c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone())
        c.execute("UPDATE places SET inspect_status='done',raw=jsonb_set(raw,'{_serp_complete}','true') WHERE id=%s",(uid,))
        pipeline.ingest(c,{**a,'phone':'622987654'},'gandia',jid)
        row=c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone()
        pipeline.add_search(c,jid,row)
        assert row['raw']['_search_generation']==1 and row['raw']['_serp_complete'] is False
        assert c.execute("SELECT count(*) n FROM passes WHERE kind='serp'").fetchone()['n']==2
        pipeline.ingest(c,{**a,'title':'Sushi Luna','categoryName':'Sushi restaurant'},'gandia',jid)
        row=c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone()
        pipeline.add_search(c,jid,row)
        row=c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone()
        assert row['inspection']['qualification']['status']=='Не підходить'


def test_city_with_pending_work_is_not_blocked_by_another_city_waiting_for_review(monkeypatch):
    monkeypatch.setattr(pipeline,'refresh_crm',lambda:None)
    first=pipeline.create_job('gandia')
    with db.connect() as c:c.execute("UPDATE passes SET status='done' WHERE job_id=%s",(first,))
    second=pipeline.create_job('daimus')
    class Fake:
        def budget(self):return {'available_usd':1}
        def minimum_cap(self,actor):return 0.5
        def start(self,*args):return {'id':'next-city-run','defaultDatasetId':'fixture'}
    monkeypatch.setattr(pipeline,'Apify',Fake)
    assert pipeline.discovery()['started']=='next-city-run'
    with db.connect() as c:assert c.execute("SELECT job_id FROM passes WHERE run_id='next-city-run'").fetchone()['job_id']==uuid.UUID(second)


def test_pending_recheck_publishes_uncertainty_without_destroying_previous_evidence():
    with db.connect() as c:
        uid=pipeline.ingest(c,{'title':'Pizza Luna','categoryName':'Pizzeria','phone':'622123456','address':'Street 1'},'gandia')
        prior={'website_status':'not_found','ordering_status':'not_found','qualification':{'priority':'A','status':'Новий','reason':'Попередня перевірка'}}
        c.execute('UPDATE places SET inspection=%s WHERE id=%s',(Jsonb(prior),uid))
        row=c.execute('SELECT * FROM places WHERE id=%s',(uid,)).fetchone()
    fields=pipeline.airtable_fields(row)
    assert fields['Пріоритет']=='C'
    assert fields['Стан сайту']=='Не вдалося перевірити' and fields['Власні онлайн-замовлення']=='Не вдалося перевірити'
    assert row['inspection']['qualification']['priority']=='A'


def test_fresh_do_not_contact_status_stops_writes_and_repeated_api_reads(monkeypatch):
    with db.connect() as c:
        uid=pipeline.ingest(c,{'title':'Pizza Luna','categoryName':'Pizzeria','phone':'622123456','address':'Street 1'},'gandia')
        c.execute("UPDATE places SET airtable_id='rec-existing' WHERE id=%s",(uid,))
    reads=[]
    class Fake:
        def records(self):
            reads.append(True)
            return [{'id':'rec-existing','fields':{'Cortex ID':str(uid),'Назва':'Pizza Luna','Телефон':'622123456','Статус':'Не контактувати'}}]
        def upsert(self,*args):raise AssertionError('Suppressed record was updated')
    monkeypatch.setattr(pipeline,'refresh_crm',lambda:None)
    monkeypatch.setattr(pipeline,'Airtable',Fake)
    pipeline.sync()
    assert pipeline.sync()['status']=='no_changes' and len(reads)==1
