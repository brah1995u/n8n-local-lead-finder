import os
import pytest
from datetime import datetime,timezone
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from app import db,pipeline
from app.main import app


def test_manual_dry_run_never_calls_external_services(monkeypatch):
    pipeline.create_job("gandia",pilot=True)
    def forbidden(*args,**kwargs):raise AssertionError("External service invoked during dry run")
    monkeypatch.setattr(pipeline,"Apify",forbidden)
    monkeypatch.setattr(pipeline,"Airtable",forbidden)
    with TestClient(app) as client:
        response=client.post("/internal/tick/discovery?dry_run=true",headers={"Authorization":"Bearer "+os.environ["INTERNAL_TOKEN"]})
    assert response.status_code==200
    assert response.json()["read_only"] is True
    assert response.json()["queued_jobs"]==1


def test_stale_legacy_cannot_replace_fresh_maps_data():
    raw={"title":"Pizza Luna","placeId":"ChIJone","phone":"622123456","address":"Street 1","categoryName":"Pizzeria","website":"https://new.example","_maps_checked_at":"2026-10-28"}
    with db.connect() as c:
        uid=pipeline.ingest(c,raw,"gandia")
        pipeline.ingest(c,{**raw,"website":"https://old.example","_legacy_only":True},"gandia")
        assert c.execute("SELECT raw FROM places WHERE id=%s",(uid,)).fetchone()["raw"]["website"]=="https://new.example"


def test_pilot_does_not_queue_unbounded_serp_queries():
    jid=pipeline.create_job("gandia",pilot=True)
    with db.connect() as c:
        for i in range(30):
            raw={"title":f"Pizza Luna {i}","placeId":f"ChIJ{i}","phone":"622123456","address":f"Street {i}","categoryName":"Pizzeria"}
            uid=pipeline.ingest(c,raw,"gandia",jid)
            place=c.execute("SELECT * FROM places WHERE id=%s",(uid,)).fetchone()
            pipeline.add_search(c,jid,place)
        assert c.execute("SELECT count(*) AS n FROM passes WHERE job_id=%s AND kind='serp'",(jid,)).fetchone()["n"]==20


def test_opaque_browser_origin_still_requires_valid_csrf():
    from bs4 import BeautifulSoup
    with TestClient(app) as client:
        bad=client.post("/jobs",data={"city_id":"gandia","mode":"start","csrf_token":"invalid"},headers={"Origin":"null"})
        assert bad.status_code==403
        html=client.get("/")
        token=BeautifulSoup(html.text,"html.parser").select_one('input[name="csrf_token"]')["value"]
        good=client.post("/jobs",data={"city_id":"gandia","mode":"start","csrf_token":token},headers={"Origin":"null"},follow_redirects=False)
        assert good.status_code==303
    with db.connect() as c:assert c.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]==1


def test_restart_requeues_an_interrupted_local_browser_check():
    with db.connect() as c:
        uid=pipeline.ingest(c,{'title':'Pizza Luna','categoryName':'Pizzeria','phone':'622123456','address':'Street 1'},'gandia')
        c.execute("UPDATE places SET inspect_status='running' WHERE id=%s",(uid,))
    with TestClient(app) as client:assert client.get('/health').status_code==200
    with db.connect() as c:assert c.execute('SELECT inspect_status FROM places WHERE id=%s',(uid,)).fetchone()['inspect_status']=='pending'


def test_budget_pause_still_finishes_already_collected_pairs(monkeypatch):
    jid=pipeline.create_job('gandia')
    with db.connect() as c:
        uid=pipeline.ingest(c,{'title':'Pizza Luna','categoryName':'Pizzeria','phone':'622123456','address':'Street 1','_serp_complete':True},'gandia',jid)
        c.execute("UPDATE jobs SET status='paused',reason='Безкоштовні кредити вичерпано' WHERE id=%s",(jid,))
    async def inspected(*args):return {'website_status':'not_found','ordering_status':'not_found','delivery':True}
    monkeypatch.setattr(pipeline,'inspect',inspected)
    assert pipeline.enrichment()['checked']==1
    with db.connect() as c:
        p=c.execute('SELECT inspect_status,inspection FROM places WHERE id=%s',(uid,)).fetchone()
    assert p['inspect_status']=='done' and p['inspection']['qualification']['priority']=='A'


@pytest.mark.parametrize('selected,label,expected',[('gandia','Grau i Platja',True),('gandia','Real de Gandia',False),('daimus','Gandia',False),('daimus','Playa de Daimús',True),('javea','Jávea',True),('villajoyosa','Villajoyosa',True),('alicante','Alacant',True)])
def test_city_filter_is_applied_to_every_selected_city(selected,label,expected):
    assert pipeline.within_city({'city':label,'countryCode':'ES'},selected) is expected
    assert not pipeline.within_city({'city':label,'countryCode':'AR'},selected)
