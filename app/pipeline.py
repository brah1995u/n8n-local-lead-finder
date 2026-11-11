import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import db
from .config import CITIES, SETTINGS, city
from .inspector import inspect, fetch_page, merchant_listing
from .rules import STATES, SUPPRESSED, dedupe, niche, norm, phone, qualify, suppression_keys, marketplace
from .services import Apify, Airtable, ServiceError

TERMS = ["pizzería", "hamburguesería", "kebab", "comida rápida", "menjar per emportar"]


def polygon(bbox):
    w, s, e, n = bbox
    return {"type": "Polygon", "coordinates": [[[w,s],[e,s],[e,n],[w,n],[w,s]]]}


def within_city(raw,city_id):
    if raw.get('countryCode') and raw['countryCode'].upper()!='ES':return False
    label=norm(raw.get('city',''))
    if not label:return True
    selected=city(city_id)
    allowed={norm(selected['query'].split(',')[0])}
    allowed.update(norm(x) for x in selected['name'].split('+')[0].split('/'))
    allowed.update(norm(x) for x in selected.get('aliases',[]))
    label=re.sub(r'^(?:platja|playa) de\s+','',label)
    return label in allowed


def insert_pass(c, job_id, key, kind, data):
    c.execute("INSERT INTO passes(job_id,key,kind,input) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (job_id, key, kind, Jsonb(data)))


def ingest(c, raw, city_id, job_id=None):
    name = raw.get("title") or raw.get("name")
    if not name:
        return None
    key, tel = dedupe(raw), phone(raw.get("phone"))
    pid = raw.get("placeId") or raw.get("place_id")
    existing = c.execute("SELECT * FROM places WHERE dedupe_key=%s OR (place_id IS NOT NULL AND place_id=%s)", (key,pid)).fetchone()
    if not niche(raw) and not existing:return None
    if not existing and tel and raw.get("address"):
        candidates = c.execute("SELECT * FROM places WHERE phone=%s AND city_id=%s", (tel,city_id)).fetchall()
        existing = next((p for p in candidates if norm(p["address"]) == norm(raw["address"]) and norm(p["name"]) == norm(name)), None)
    uid = existing["id"] if existing else uuid.uuid4()
    if existing:
        if raw.get("_legacy_only") and existing["raw"].get("_maps_checked_at"):
            if job_id:c.execute("INSERT INTO job_places VALUES(%s,%s) ON CONFLICT DO NOTHING", (job_id,uid))
            return uid
        merged = {**existing["raw"], **raw}
        changed=bool(raw.get('_maps_checked_at')) and any(existing['raw'].get(k)!=merged.get(k) for k in ('title','name','phone','address','website','categories','categoryName','additionalInfo','permanentlyClosed','temporarilyClosed','_coastal','description'))
        if changed and (norm(existing['name'])!=norm(name) or existing['phone']!=tel):
            merged.update(_serp_complete=False,_serp_results=[],_serp_queries_seen=[],_search_generation=int(existing['raw'].get('_search_generation',0))+1)
        c.execute("UPDATE places SET raw=%s,name=%s,phone=%s,address=%s,place_id=COALESCE(%s,place_id),inspect_status=CASE WHEN %s THEN 'pending' ELSE inspect_status END,updated_at=now() WHERE id=%s", (Jsonb(merged),name,tel,raw.get("address", existing["address"]),pid,changed,uid))
    else:
        c.execute("INSERT INTO places(id,dedupe_key,city_id,name,phone,address,place_id,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (uid,key,city_id,name,tel,raw.get("address", ""),pid,Jsonb(raw)))
    if job_id:
        c.execute("INSERT INTO job_places VALUES(%s,%s) ON CONFLICT DO NOTHING", (job_id,uid))
    return uid


def add_search(c, job_id, place):
    excluded = qualify(place["raw"],{},suppression_check(c,place["raw"]))
    if excluded["status"] in ("Не контактувати","Не підходить"):
        c.execute("UPDATE places SET inspect_status='done',inspection=%s WHERE id=%s", (Jsonb({"qualification":excluded}),place["id"]))
        return
    if place["raw"].get("_legacy_only"):
        return
    if place["raw"].get("_serp_complete"):
        return
    job=c.execute("SELECT pilot_limit FROM jobs WHERE id=%s",(job_id,)).fetchone()
    if job and job["pilot_limit"]:
        count=c.execute("SELECT count(*) AS n FROM passes WHERE job_id=%s AND kind='serp'",(job_id,)).fetchone()["n"]
        if count>=job["pilot_limit"]:return
    raw = place["raw"]
    query_name = str(raw.get("title") or raw.get("name", "")).split("|")[0].replace('"', ' ').replace("\n", " ").strip()
    city_name = city(place["city_id"])["name"].split(" + ")[0]
    query_phone = phone(raw.get("phone"))
    queries = [f'{query_name} {city_name} sitio oficial', f'"{query_phone[-9:] if query_phone else query_name}" {city_name} contacto pedidos']
    generation=raw.get('_search_generation',0)
    suffix=':generation-'+str(generation) if generation else ''
    insert_pass(c, job_id, "serp:" + str(place["id"])+suffix, "serp", {"queries": "\n".join(queries), "maxPagesPerQuery": 1, "resultsPerPage": 10, "countryCode": "es", "languageCode": "es", "_place_id": str(place["id"])})


def create_job(city_id, mode="start", pilot=False):
    selected = city(city_id)
    with db.lock("job-control") as c:
        if not c:
            raise ServiceError("Інший запуск уже обробляється", "control")
        previous = c.execute("SELECT * FROM jobs WHERE city_id=%s ORDER BY created_at DESC LIMIT 1", (city_id,)).fetchone()
        if mode == "continue":
            if not previous or previous["status"] == "complete":
                raise ServiceError("Немає незавершеного збору цього міста", "control")
            c.execute("UPDATE jobs SET status='queued',reason=NULL,pilot_limit=NULL,updated_at=now() WHERE id=%s", (previous["id"],))
            for p in c.execute("SELECT * FROM passes WHERE job_id=%s AND kind='maps' AND input->>'_pilot'='true'", (previous["id"],)).fetchall():
                data = dict(p["input"])
                data.pop("_pilot", None)
                data["maxCrawledPlacesPerSearch"] = 120
                data['scrapePlaceDetailPage']=True
                insert_pass(c, previous["id"], p["key"] + ":full", "maps", data)
                if p['status']=='pending' and not p['run_id']:
                    c.execute("UPDATE passes SET status='superseded',reason='Замінено повним проходом після пілота' WHERE id=%s",(p['id'],))
            # API-declined requests can be retried by an explicit Continue; ambiguous starts are reconciled only.
            for failed in c.execute("SELECT * FROM passes WHERE job_id=%s AND status='failed' AND run_id IS NOT NULL",(previous['id'],)).fetchall():
                payload={k:v for k,v in failed['input'].items() if k not in ('_batch','_result_counts','_actor_terminal_status')}
                own=next((entry for entry in failed['input'].get('_batch',[]) if entry['pass_id']==failed['id']),None)
                if own:
                    payload['queries']='\n'.join(own['queries'])
                    payload['_place_id']=own['place_id']
                insert_pass(c,previous['id'],failed['key']+':retry','serp' if failed['kind']=='serp' else failed['kind'],payload)
                c.execute("UPDATE passes SET status='superseded',reason='Повторний прохід після явного Продовжити; історію runId і витрат збережено' WHERE id=%s",(failed['id'],))
            c.execute("UPDATE passes SET status='pending',reason=NULL WHERE job_id=%s AND status='failed' AND run_id IS NULL", (previous["id"],))
            c.execute("UPDATE coverage_pages SET status='pending',reason=NULL WHERE job_id=%s AND status='failed'",(previous['id'],))
            return str(previous["id"])
        if previous and previous["status"] in ("queued", "running", "paused", "pilot_complete") and mode == "start":
            raise ServiceError("Збір уже існує. Натисни «Продовжити»", "control")
        job_id = uuid.uuid4()
        c.execute("INSERT INTO jobs(id,city_id,city_name,mode,pilot_limit) VALUES(%s,%s,%s,%s,%s)", (job_id,city_id,selected["name"],mode,20 if pilot else None))
        for p in c.execute("SELECT * FROM places WHERE city_id=%s", (city_id,)).fetchall():
            c.execute("INSERT INTO job_places VALUES(%s,%s) ON CONFLICT DO NOTHING", (job_id,p["id"]))
            if mode == "recheck":
                raw = dict(p["raw"])
                raw.pop("_serp_complete", None)
                raw.pop("_serp_results", None)
                c.execute("UPDATE places SET inspect_status='pending',attempts=0,raw=%s WHERE id=%s", (Jsonb(raw),p["id"]))
                p["raw"] = raw
            if p["inspect_status"] != "done" or mode == "recheck":
                add_search(c,job_id,p)
        for zone in selected.get("zones", [{"name":selected["name"]}]):
            payload = {"searchStringsArray": TERMS, "maxCrawledPlacesPerSearch": 5 if pilot else 120, "language": "es", "website": "allPlaces", "scrapePlaceDetailPage": True, "scrapeContacts": False, "scrapeReviewsPersonalData": False, "maxReviews": 0, "maxImages": 0, "_zone":zone, "_depth":0}
            if "bbox" in zone and not zone.get("location_query"):
                payload["customGeolocation"] = polygon(zone["bbox"])
            else:
                payload["locationQuery"] = selected["query"]
            if pilot:
                payload["_pilot"] = True
            insert_pass(c,job_id,"maps:" + zone["name"],"maps",payload)
        legacy_ids=[p["place_id"] for p in c.execute("SELECT place_id FROM places WHERE city_id=%s AND raw->>'_legacy_only'='true' AND place_id IS NOT NULL",(city_id,)).fetchall()]
        if legacy_ids:
            insert_pass(c,job_id,"maps:legacy-refresh","maps",{"placeIds":legacy_ids,"language":"es","scrapePlaceDetailPage":False,"scrapeContacts":False,"maxReviews":0,"scrapeReviewsPersonalData":False,"_zone":{"name":"Перевірка старих Google Place ID"}})
        for term in ("pizzería hamburguesería kebab site:glovoapp.com", "pizza burger kebab site:just-eat.es"):
            insert_pass(c,job_id,"coverage:"+term,"coverage",{"queries":f'{term} "{selected["name"].split(" + ")[0]}"',"maxPagesPerQuery":2,"resultsPerPage":10,"countryCode":"es","languageCode":"es"})
        return str(job_id)


def suppression_check(c, raw):
    return bool(c.execute("SELECT 1 FROM suppressions WHERE key=ANY(%s) LIMIT 1", (suppression_keys(raw),)).fetchone())


def refresh_crm(force=False):
    last = db.get("crm_refresh_at")
    if not force and last and datetime.fromisoformat(last) > datetime.now(timezone.utc)-timedelta(hours=24):
        return
    airtable = Airtable()
    rows = airtable.records()
    with db.connect() as c:
        for row in rows:
            f = row["fields"]
            uid = f.get("Cortex ID")
            if uid:
                c.execute("UPDATE places SET airtable_id=%s,crm_status=%s WHERE id::text=%s", (row["id"],f.get("Статус"),uid))
            if norm(f.get("Статус")) in SUPPRESSED:
                raw = {"title":f.get("Назва"),"phone":f.get("Телефон"),"address":f.get("Адреса")}
                for key in suppression_keys(raw):
                    c.execute("INSERT INTO suppressions(key,reason,source) VALUES(%s,%s,%s) ON CONFLICT(key) DO UPDATE SET reason=excluded.reason,updated_at=now()", (key,f["Статус"],"current CRM"))
        for source in SETTINGS["legacy_sources"]:
            # Initial migration is required before discovery so previous refusals are respected.
            legacy = airtable.records(source["base"],source["table"])
            for row in legacy:
                f = row["fields"]
                status = f.get("Status", f.get("Статус", ""))
                raw = {"title":f.get("Name", f.get("Ресторан", "")),"phone":f.get("Phone", f.get("Телефон", "")),"address":f.get("Address", ""),"placeId":f.get("Place ID"),"website":f.get("Website", f.get("Сайт", "")),"categoryName":f.get("Category", f.get("Ніша", "")),"url":f.get("Google Maps URL", f.get("Джерело даних", "")),"_legacy_id":row["id"],"_legacy_only":True}
                with db.connect() as c:
                    if f.get("Opt-out") or norm(status) in SUPPRESSED:
                        for key in suppression_keys(raw):
                            c.execute("INSERT INTO suppressions(key,reason,source) VALUES(%s,%s,%s) ON CONFLICT(key) DO UPDATE SET reason=excluded.reason,updated_at=now()", (key,"Попередній статус: "+status,source["base"]))
                    if source["kind"] == "maps" or "gand" in norm(f.get("Місто", "")):
                        ingest(c,raw,"gandia")
    db.put("airtable_record_count",len(rows))
    db.put("crm_refresh_at",datetime.now(timezone.utc).isoformat())
    db.put("legacy_imported",True)


def _reconcile(api,p):
    actor = SETTINGS["maps_actor"] if p["kind"] == "maps" else SETTINGS["search_actor"]
    expected = {k:v for k,v in p["input"].items() if not k.startswith("_")}
    candidates = []
    runs = api.get(f"acts/{actor}/runs",limit=20,desc="true").get("items",[])
    for run in runs:
        started = datetime.fromisoformat(run["startedAt"].replace("Z","+00:00"))
        if started < p["updated_at"]-timedelta(seconds=2):
            continue
        original = api.get(f"key-value-stores/{run['defaultKeyValueStoreId']}/records/INPUT")
        # Actors may add default fields to INPUT. Every submitted field still has to match.
        if all(original.get(k) == v for k,v in expected.items()):
            candidates.append(run)
    if len(candidates) == 1:
        with db.connect() as c:
            c.execute("UPDATE passes SET status='running',run_id=%s,dataset_id=%s,reason=NULL,updated_at=now() WHERE id=%s", (candidates[0]["id"],candidates[0]["defaultDatasetId"],p["id"]))
        return {"reconciled":candidates[0]["id"]}
    raise ServiceError("Неоднозначний запуск Apify. Потрібно звірити runId у Console; новий запуск заблокований", "discovery")


def _split(c,p):
    zone = p["input"].get("_zone",{})
    depth = p["input"].get("_depth",0)
    if not zone.get("bbox") or depth >= 3:
        c.execute("UPDATE passes SET status='capped',reason='Ліміт результатів/вартості: потрібен детальніший географічний прохід' WHERE id=%s", (p["id"],))
        return
    w,s,e,n = zone["bbox"]
    mx,my = (w+e)/2,(s+n)/2
    for i,bounds in enumerate([[w,s,mx,my],[mx,s,e,my],[w,my,mx,n],[mx,my,e,n]]):
        payload = dict(p["input"])
        payload.pop("locationQuery",None)
        payload["_zone"] = {**zone,"bbox":bounds,"location_query":False,"name":zone["name"]+f" / {i+1}"}
        payload["_depth"] = depth+1
        payload["customGeolocation"] = polygon(bounds)
        insert_pass(c,p["job_id"],p["key"]+f":tile{i}","maps",payload)
    c.execute("UPDATE passes SET status='superseded',reason='Район розділено на чотири менші проходи' WHERE id=%s", (p["id"],))


def discovery():
    with db.lock("discovery-global") as lock:
        if not lock:
            return {"status":"busy"}
        refresh_crm()
        if db.get("airtable_record_count",0) >= SETTINGS["record_limit"]:
            raise ServiceError("У Airtable 800 записів: новий збір призупинено", "discovery")
        with db.connect() as c:
            p = c.execute("SELECT p.*,j.city_id,j.city_name,j.pilot_limit FROM passes p JOIN jobs j ON j.id=p.job_id WHERE p.status IN ('running','starting','uncertain') ORDER BY p.id LIMIT 1").fetchone()
            if not p:
                job = c.execute("SELECT j.* FROM jobs j WHERE status IN ('queued','running') ORDER BY CASE WHEN EXISTS(SELECT 1 FROM passes p WHERE p.job_id=j.id AND p.status='pending') THEN 0 ELSE 1 END,created_at LIMIT 1").fetchone()
                if not job:
                    return {"status":"idle"}
                if job["pilot_limit"]:
                    done = c.execute("SELECT count(*) AS n FROM job_places jp JOIN places p ON p.id=jp.place_id WHERE jp.job_id=%s AND p.inspect_status='done' AND p.raw ? '_maps_checked_at'", (job["id"],)).fetchone()["n"]
                    if done >= job["pilot_limit"]:
                        c.execute("UPDATE jobs SET status='pilot_complete',reason='Пілот завершено: перевірити 20 карток перед повним проходом',updated_at=now() WHERE id=%s", (job["id"],))
                        return {"status":"pilot_complete"}
                # Attach migrated candidates to this city's job and search them once.
                for place in c.execute("SELECT * FROM places WHERE city_id=%s AND inspect_status='pending'", (job["city_id"],)).fetchall():
                    c.execute("INSERT INTO job_places VALUES(%s,%s) ON CONFLICT DO NOTHING", (job["id"],place["id"]))
                    add_search(c,job["id"],place)
                # A refusal or chain exclusion may arrive after the search was queued.
                for queued in c.execute("SELECT s.id search_id,p.* FROM passes s JOIN places p ON p.id::text=s.input->>'_place_id' WHERE s.job_id=%s AND s.kind='serp' AND s.status='pending'",(job['id'],)).fetchall():
                    excluded=qualify(queued['raw'],{},suppression_check(c,queued['raw']))
                    if excluded['status'] in ('Не контактувати','Не підходить'):
                        c.execute("UPDATE passes SET status='superseded',reason=%s WHERE id=%s",(excluded['reason'],queued['search_id']))
                p = c.execute("SELECT p.*,j.city_id,j.city_name,j.pilot_limit FROM passes p JOIN jobs j ON j.id=p.job_id WHERE p.job_id=%s AND p.status='pending' ORDER BY CASE WHEN p.kind='serp' THEN 0 WHEN p.kind='maps' AND COALESCE((p.input->>'_depth')::int,0)=0 AND (COALESCE(p.input->>'_pilot','false')='true' OR COALESCE(p.input->'_zone'->>'location_query','false')<>'true') THEN 1 WHEN p.kind='coverage' THEN 2 WHEN p.kind='maps' AND COALESCE((p.input->>'_depth')::int,0)=0 THEN 3 ELSE 4 END,p.id LIMIT 1", (job["id"],)).fetchone()
                if p and job["pilot_limit"] and p["kind"]!="serp":
                    assigned=c.execute("SELECT count(*) AS n FROM passes WHERE job_id=%s AND kind='serp'",(job["id"],)).fetchone()["n"]
                    if assigned>=job["pilot_limit"]:return {"status":"awaiting_pilot_verification"}
                if p and p["kind"] == "serp" and not p["input"].get("_batch"):
                    group=c.execute("SELECT * FROM passes WHERE job_id=%s AND kind='serp' AND status='pending' ORDER BY id LIMIT 10",(job["id"],)).fetchall()
                    payload=dict(p["input"])
                    payload["_batch"]=[{"pass_id":x["id"],"place_id":x["input"]["_place_id"],"queries":x["input"]["queries"].splitlines()} for x in group]
                    payload["queries"]="\n".join(q for entry in payload["_batch"] for q in entry["queries"])
                    c.execute("UPDATE passes SET input=%s WHERE id=%s",(Jsonb(payload),p["id"]))
                    c.execute("UPDATE passes SET status='batched',reason=%s WHERE id=ANY(%s) AND id<>%s",("Входить у пакет "+str(p["id"]),[x["id"] for x in group],p["id"]))
                    p["input"]=payload
                c.execute("UPDATE jobs SET status='running',reason=NULL,updated_at=now() WHERE id=%s", (job["id"],))
            if not p:
                return {"status":"awaiting_verification"}
        api = Apify()
        if p["status"] in ("starting","uncertain"):
            return _reconcile(api,p)
        if p["status"] == "pending":
            budget = api.budget()
            cap = min(SETTINGS["apify_run_cap_usd"],budget["available_usd"])
            actor = SETTINGS["maps_actor"] if p["kind"] == "maps" else SETTINGS["search_actor"]
            minimum = api.minimum_cap(actor)
            if cap < minimum:
                with db.connect() as c:
                    reason = f"Безкоштовні кредити недостатні для мінімального ліміту запуску ${minimum:.2f}; доступно ${cap:.2f} після резерву. Відновлення після " + budget["cycle"]["endAt"]
                    c.execute("UPDATE jobs SET status='paused',reason=%s,updated_at=now() WHERE id=%s", (reason,p["job_id"]))
                return {"status":"paused_budget"}
            with db.connect() as c:
                c.execute("UPDATE passes SET status='starting',reserved_usd=%s,attempts=attempts+1,updated_at=now() WHERE id=%s", (cap,p["id"]))
            try:
                run = api.start(actor,{k:v for k,v in p["input"].items() if not k.startswith("_")},cap)
            except ServiceError as e:
                with db.connect() as c:
                    c.execute("UPDATE passes SET status=%s,reason=%s WHERE id=%s", ("uncertain" if e.ambiguous else "failed",str(e),p["id"]))
                raise
            with db.connect() as c:
                c.execute("UPDATE passes SET status='running',run_id=%s,dataset_id=%s,updated_at=now() WHERE id=%s", (run["id"],run["defaultDatasetId"],p["id"]))
            return {"started":run["id"]}
        run = api.get(f"actor-runs/{p['run_id']}")
        terminal=run['status'] in ('SUCCEEDED','FAILED','ABORTED','TIMED-OUT')
        if not terminal and p['kind']!='serp':
            return {"status":run["status"],"run_id":p["run_id"]}
        items = api.get(f"datasets/{p['dataset_id']}/items",offset=p["offset_count"],limit=100,clean="true")
        with db.connect() as c:
            counts=dict(p['input'].get('_result_counts',{}))
            if items:
                for item in items:
                    if p["kind"] == "maps":
                        term=item.get('searchString') or '__unknown__'
                        counts[term]=counts.get(term,0)+1
                        if not within_city(item,p['city_id']):
                            continue
                        raw = {**item,"_coastal":bool(p["input"].get("_zone",{}).get("coastal")) or str(item.get("postalCode"))=="46730","_sources":[item.get("url","")],"_legacy_only":False,"_maps_checked_at":datetime.now(timezone.utc).isoformat()}
                        loc, zone = item.get("location",{}), p["input"].get("_zone",{})
                        if not item.get('city') and not zone.get('bbox'):
                            raw['_review_reason']='Потрібно підтвердити місто за адресою перед дзвінком.'
                        if zone.get("bbox") and loc.get("lat") is not None and loc.get("lng") is not None:
                            w,s,e,n = zone["bbox"]
                            if not (w <= loc["lng"] <= e and s <= loc["lat"] <= n):
                                continue
                        uid = ingest(c,raw,p["city_id"],p["job_id"])
                        if uid:
                            place = c.execute("SELECT * FROM places WHERE id=%s", (uid,)).fetchone()
                            if place["inspect_status"] != "done":
                                add_search(c,p["job_id"],place)
                    elif p["kind"] == "serp":
                        if not isinstance(item.get('organicResults'),list) or item.get('error'):
                            continue
                        query=item.get("searchQuery",{})
                        term=query.get("term",query.get("query","")) if isinstance(query,dict) else str(query)
                        for entry in p["input"].get("_batch",[{"place_id":p["input"]["_place_id"],"queries":p["input"]["queries"].splitlines()}]):
                            if term and any(norm(term)==norm(q) for q in entry["queries"]):
                                uid=entry["place_id"]
                                c.execute("UPDATE places SET raw=jsonb_set(jsonb_set(raw,'{_serp_results}',COALESCE(raw->'_serp_results','[]'::jsonb) || %s::jsonb),'{_serp_queries_seen}',COALESCE(raw->'_serp_queries_seen','[]'::jsonb) || %s::jsonb),updated_at=now() WHERE id=%s", (Jsonb(item.get("organicResults",[])),Jsonb([norm(term)]),uid))
                    else:
                        # Keep marketplace coverage evidence for inspection and explicit coverage reporting.
                        db.put(f"coverage:{p['id']}:{p['offset_count']}",item.get("organicResults",[]))
                        for result in item.get('organicResults',[]):
                            url=result.get('url','')
                            if marketplace(url):
                                c.execute('INSERT INTO coverage_pages(job_id,url) VALUES(%s,%s) ON CONFLICT DO NOTHING',(p['job_id'],url))
                c.execute("UPDATE passes SET offset_count=offset_count+%s,result_count=result_count+%s,updated_at=now() WHERE id=%s", (len(items),len(items),p["id"]))
                if p['kind']=='maps':
                    p['input']['_result_counts']=counts
                    c.execute('UPDATE passes SET input=%s WHERE id=%s',(Jsonb(p['input']),p['id']))
                if len(items)==100:
                    return {"ingested":len(items),"run_id":p["run_id"]}
                p["result_count"]+=len(items)
            cost = float(run.get("usageTotalUsd") or 0)
            if terminal:
                c.execute("UPDATE passes SET status=%s,cost_usd=%s,input=jsonb_set(input,'{_actor_terminal_status}',to_jsonb(%s::text)),updated_at=now() WHERE id=%s", ("done" if run["status"] == "SUCCEEDED" else "failed",cost,run['status'],p["id"]))
            if p["kind"] == "serp":
                for entry in p["input"].get("_batch",[{"pass_id":p["id"],"place_id":p["input"]["_place_id"],"queries":p["input"]["queries"].splitlines()}]):
                    place=c.execute("SELECT raw FROM places WHERE id=%s",(entry["place_id"],)).fetchone()
                    seen=set(place["raw"].get("_serp_queries_seen",[]))
                    complete=all(norm(q) in seen for q in entry["queries"])
                    if complete:c.execute("UPDATE places SET raw=jsonb_set(raw,'{_serp_complete}','true'::jsonb) WHERE id=%s",(entry["place_id"],))
                    # Keep the parent running until Apify stops, while independent
                    # completed query pairs can already be checked locally.
                    if terminal or (complete and entry['pass_id']!=p['id']):
                        c.execute("UPDATE passes SET status=%s,reason=%s WHERE id=%s",("done" if complete else "failed",None if complete else "Пошук повернув не всі зіставлені запити; відсутність сайту не підтверджується",entry["pass_id"]))
            if not terminal:return {'status':run['status'],'run_id':p['run_id'],'ingested':len(items)}
            if p["kind"] == "maps" and run["status"] == "SUCCEEDED":
                cap_hit = cost >= float(p["reserved_usd"])*0.95 or any(n >= p["input"].get("maxCrawledPlacesPerSearch",120) for n in counts.values())
                if cap_hit and not p["input"].get("_pilot"):
                    _split(c,p)
        return {"status":"processed", "run_id":p["run_id"]}


def check_coverage():
    with db.connect() as c:
        entry=c.execute("SELECT cp.*,j.city_id FROM coverage_pages cp JOIN jobs j ON j.id=cp.job_id WHERE cp.status='pending' AND j.status IN ('queued','running','paused') ORDER BY cp.id LIMIT 1").fetchone()
    if not entry:return {'coverage':'idle'}
    page=asyncio.run(fetch_page(entry['url']))
    with db.connect() as c:
        if not page:
            c.execute("UPDATE coverage_pages SET status='failed',reason='Публічний список недоступний: потрібна ручна перевірка покриття' WHERE id=%s",(entry['id'],))
            return {'coverage':'unavailable'}
        links=[l for l in page['links'] if merchant_listing(l['url'])]
        if merchant_listing(entry['url']):links.insert(0,{'url':entry['url'],'text':page['text'][:100]})
        names=[]
        for link in links:
            name=link['text'].split('\n')[0].strip()
            if len(name)>90 or len(name)<3:continue
            name=re.split(r'Gratis|Cerrado|Programar|\d+%|a domicilio|Menú|Menu',name,flags=re.I)[0].strip(' -|')
            if len(name)>=3 and name not in names:names.append(name)
        selected=city(entry['city_id'])
        if names:
            insert_pass(c,entry['job_id'],'maps:coverage:'+str(entry['id']),'maps',{'searchStringsArray':names[:60],'locationQuery':selected['query'],'language':'es','maxCrawledPlacesPerSearch':3,'website':'allPlaces','scrapePlaceDetailPage':True,'scrapeReviewsPersonalData':False,'maxReviews':0,'maxImages':0,'_zone':{'name':'Зіставлення публічного списку','bbox':selected.get('zones',[{}])[0].get('bbox')} ,'_coverage_source':entry['url']})
        c.execute("UPDATE coverage_pages SET status='done',candidates=%s WHERE id=%s",(len(names),entry['id']))
    return {'coverage':'checked','candidates':len(names)}


def enrichment():
    with db.lock("enrichment") as lock:
        if not lock:
            return {"status":"busy"}
        with db.connect() as c:
            c.execute("UPDATE places SET inspect_status='pending' WHERE inspect_status='running' AND updated_at < now()-interval '10 minutes'")
            rows = c.execute("SELECT DISTINCT p.* FROM places p JOIN job_places jp ON p.id=jp.place_id JOIN jobs j ON j.id=jp.job_id WHERE j.status IN ('queued','running','paused') AND p.inspect_status='pending' AND p.raw->>'_serp_complete'='true' ORDER BY p.created_at LIMIT 1").fetchall()
            for p in rows:
                c.execute("UPDATE places SET inspect_status='running',attempts=attempts+1,updated_at=now() WHERE id=%s", (p["id"],))
        for p in rows:
            try:
                inspection = asyncio.run(inspect(p["raw"],p["raw"].get("_serp_results",[])+p["raw"].get('_manual_evidence',[]),True))
                with db.connect() as c:
                    inspection["qualification"] = qualify(p["raw"],inspection,suppression_check(c,p["raw"]))
                    c.execute("UPDATE places SET inspection=%s,inspect_status='done',updated_at=now() WHERE id=%s", (Jsonb(inspection),p["id"]))
            except Exception:
                with db.connect() as c:
                    c.execute("UPDATE places SET inspect_status=%s,updated_at=now() WHERE id=%s", ("failed" if p["attempts"] >= 2 else "pending",p["id"]))
                db.event("enrichment","Перевірку закладу не завершено; запис збережений у черзі","warning")
        if not rows:return check_coverage()
        return {"checked":len(rows)}


def airtable_fields(p):
    i = p["inspection"] or {}
    q = i.get('qualification',{'priority':'C','status':'Перевірити','reason':'Потрібна перевірка'})
    if p.get('inspect_status','done')!='done':
        # A stale positive result must leave the calling view while new evidence is pending.
        i={**i,'website_status':'unknown','ordering_status':'unknown','checked_at':p['updated_at'].isoformat()}
        q={**q,'priority':'C','status':'Перевірити','reason':'Повторна перевірка ще не завершена. Підтвердити сайт і власні замовлення перед дзвінком.'}
    return {"Назва":p["name"],"Місто / район":city(p["city_id"])["name"],"Ніша":q.get("niche") or niche(p["raw"]),"Телефон":p["phone"] or "","Причина звернення":q["reason"],"Пріоритет":q["priority"],"Адреса":p["address"] or "","Сайт":i.get("website",""),"Стан сайту":STATES[i.get("website_status", "unknown")],"Власні онлайн-замовлення":STATES[i.get("ordering_status", "unknown")],"Канали замовлень":i.get("channels",[]),"Джерела":"\n".join(i.get("sources",[])),"Дата перевірки":i.get("checked_at") or p["updated_at"].isoformat(),"Cortex ID":str(p["id"])}


def fingerprint(fields):
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def sync():
    with db.lock("sync") as lock:
        if not lock:
            return {"status":"busy"}
        refresh_crm()
        with db.connect() as c:
            # Recheck can disqualify an already-published lead. Refresh its research fields,
            # including priority C, while preserving the user's sales status and notes.
            candidates = c.execute("SELECT * FROM places WHERE (inspect_status='done' AND inspection->'qualification'->>'eligible'='true') OR airtable_id IS NOT NULL ORDER BY updated_at").fetchall()
        candidates = [p for p in candidates if norm(p['crm_status']) not in SUPPRESSED and fingerprint(airtable_fields(p)) != p["synced_hash"]]
        if not candidates:
            return {"status":"no_changes"}
        api = Airtable()
        remote = api.records()
        index = {r["fields"].get("Cortex ID"):r for r in remote}
        with db.connect() as c:
            for record in remote:
                f=record['fields']
                if f.get('Cortex ID'):c.execute('UPDATE places SET crm_status=%s WHERE id::text=%s',(f.get('Статус'),f['Cortex ID']))
                if norm(f.get('Статус')) in SUPPRESSED:
                    for key in suppression_keys({'title':f.get('Назва'),'phone':f.get('Телефон'),'address':f.get('Адреса')}):
                        c.execute("INSERT INTO suppressions(key,reason,source) VALUES(%s,%s,'current CRM') ON CONFLICT(key) DO UPDATE SET reason=excluded.reason",(key,f['Статус']))
        db.put("airtable_record_count",len(remote))
        writes, pending = [], []
        for p in candidates:
            with db.connect() as c:
                if suppression_check(c,p["raw"]) or norm(p["crm_status"]) in SUPPRESSED:
                    continue
            uid = str(p["id"])
            if uid in index and norm(index[uid]['fields'].get('Статус')) in SUPPRESSED:continue
            if uid not in index and len(remote)+sum(1 for x in pending if str(x["id"]) not in index) >= SETTINGS["record_limit"]:
                continue
            fields = airtable_fields(p)
            if uid not in index:
                fields["Статус"] = p["inspection"]["qualification"]["status"]
            writes.append({"fields":fields})
            pending.append(p)
            if len(writes) == 10:
                break
        if not writes:
            return {"status":"record_limit_or_suppressed"}
        result = api.upsert(writes)
        by_uid = {r["fields"]["Cortex ID"]:r for r in result.get("records",[])}
        with db.connect() as c:
            for p in pending:
                r = by_uid.get(str(p["id"]))
                if r:
                    c.execute("UPDATE places SET airtable_id=%s,synced_hash=%s,crm_status=%s WHERE id=%s", (r["id"],fingerprint(airtable_fields(p)),r["fields"].get("Статус"),p["id"]))
        db.put("airtable_record_count",len(remote)+sum(str(p["id"]) not in index for p in pending))
        return {"synced":len(by_uid)}


def maintenance():
    now = datetime.now(timezone.utc)
    budget = db.get("apify_budget",{})
    if budget.get("cycle",{}).get("endAt") and datetime.fromisoformat(budget["cycle"]["endAt"].replace("Z","+00:00")) < now:
        with db.connect() as c:
            resumed=c.execute("UPDATE jobs SET status='queued',reason=NULL WHERE status='paused' AND reason LIKE 'Безкоштовні кредити%%' RETURNING id").fetchall()
            for job in resumed:c.execute("UPDATE coverage_pages SET status='pending',reason=NULL WHERE job_id=%s AND status='failed'",(job['id'],))
    with db.connect() as c:
        jobs = c.execute("SELECT * FROM jobs WHERE status IN ('queued','running')").fetchall()
        for job in jobs:
            remaining = c.execute("SELECT count(*) AS n FROM passes WHERE job_id=%s AND status NOT IN ('done','superseded')", (job["id"],)).fetchone()["n"]
            unchecked = c.execute("SELECT count(*) AS n FROM job_places jp JOIN places p ON p.id=jp.place_id WHERE jp.job_id=%s AND p.inspect_status <> 'done'", (job["id"],)).fetchone()["n"]
            coverage=c.execute("SELECT count(*) n FROM coverage_pages WHERE job_id=%s AND status<>'done'",(job['id'],)).fetchone()['n']
            if not remaining and not unchecked and not coverage:
                c.execute("UPDATE jobs SET status='complete',reason=NULL,updated_at=now() WHERE id=%s", (job["id"],))
                from pathlib import Path
                Path("/runtime/backup-request").touch()
                db.event("coverage",job["city_name"]+": заплановані проходи завершені")
        c.execute("DELETE FROM events WHERE created_at < now()-interval '30 days'")
    return {"status":"ok"}
