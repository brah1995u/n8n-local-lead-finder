import hmac
import json
import os
import secrets as secure
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import db, pipeline
from .config import CITIES, ROOT, SETTINGS, STATE, save_secrets, secrets
from .services import Apify, Airtable, ServiceError


@asynccontextmanager
async def lifespan(app):
    db.init()
    # This Compose project runs one worker. A previous process cannot still own
    # local browser checks after its container has stopped.
    with db.connect() as c:c.execute("UPDATE places SET inspect_status='pending' WHERE inspect_status='running'")
    yield


app = FastAPI(title="Cortex Leads", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT/"app/templates"))
def localtime(value):
    try:
        dt=datetime.fromisoformat(value.replace("Z","+00:00")) if isinstance(value,str) else value
        return dt.astimezone(ZoneInfo("Europe/Madrid")).strftime("%d.%m.%Y %H:%M")
    except (ValueError,TypeError,AttributeError):return str(value)
templates.env.filters["localtime"]=localtime
TOKEN_PATH = STATE/"csrf-token"


def csrf():
    STATE.mkdir(parents=True,exist_ok=True)
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(secure.token_urlsafe(32))
        TOKEN_PATH.chmod(0o600)
    return TOKEN_PATH.read_text()


@app.middleware("http")
async def local_only(request, call_next):
    host = request.headers.get("host","").split(":")[0]
    if host not in {"localhost","127.0.0.1","worker","testserver"}:
        return JSONResponse({"error":"invalid_host"},status_code=403)
    if request.method == "POST" and not request.url.path.startswith("/internal/"):
        origin = request.headers.get("origin")
        # Opaque-origin browser previews still have to submit the unguessable CSRF token.
        # Every public POST handler below validates that token before any write.
        if origin and origin not in {"http://localhost:5680","http://127.0.0.1:5680","null"}:
            return JSONResponse({"error":"invalid_origin"},status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    return response


def check_csrf(value):
    if not hmac.compare_digest(value,csrf()):
        raise HTTPException(403,"Онови сторінку перед відправленням")


def internal(authorization: str = Header(default="")):
    if not hmac.compare_digest(authorization,"Bearer "+os.environ["INTERNAL_TOKEN"]):
        raise HTTPException(401,"unauthorized")
    if os.environ.get("RESTORE_MODE","false").lower() == "true":
        raise HTTPException(503,"Відновлена тестова копія: зовнішні дії вимкнені")


@app.exception_handler(ServiceError)
async def service_error(request,exc):
    db.event(exc.stage,str(exc),"warning")
    return JSONResponse({"error":exc.stage,"message":str(exc)},status_code=503)


@app.get("/health")
def health():
    with db.connect() as c:
        c.execute("SELECT 1")
    return {"status":"ok"}


def status_data():
    with db.connect() as c:
        jobs = c.execute("""SELECT j.*,
          (SELECT count(*) FROM job_places WHERE job_id=j.id) AS discovered,
          (SELECT count(*) FROM job_places jp JOIN places p ON p.id=jp.place_id WHERE jp.job_id=j.id AND p.inspect_status='done') AS checked,
          (SELECT count(*) FROM job_places jp JOIN places p ON p.id=jp.place_id WHERE jp.job_id=j.id AND p.inspect_status='done' AND p.inspection->'qualification'->>'eligible'='true') AS eligible,
          (SELECT count(*) FROM passes WHERE job_id=j.id AND status IN ('done','superseded')) AS passes_done,
          (SELECT count(*) FROM passes WHERE job_id=j.id) AS passes_total
          FROM jobs j ORDER BY j.created_at DESC LIMIT 20""").fetchall()
        events = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8").fetchall()
        totals = c.execute("SELECT count(*) AS all_places,count(*) FILTER (WHERE airtable_id IS NOT NULL) AS synced FROM places").fetchone()
        passes = c.execute("SELECT p.id,p.key,p.status,p.run_id,p.reason,j.city_name FROM passes p JOIN jobs j ON j.id=p.job_id WHERE p.status IN ('failed','uncertain','starting','capped') ORDER BY p.id DESC LIMIT 12").fetchall()
        coverage=c.execute("SELECT cp.url,cp.status,cp.reason,cp.candidates,j.city_name FROM coverage_pages cp JOIN jobs j ON j.id=cp.job_id ORDER BY cp.id DESC LIMIT 30").fetchall()
    backup_path = Path("/runtime/backup-status.json")
    backup = json.loads(backup_path.read_text()) if backup_path.exists() else {"status":"Не підключено"}
    return {"jobs":jobs,"events":events,"totals":totals,"passes":passes,"coverage":coverage,"budget":db.get("apify_budget"),"backup":backup}


@app.get("/",response_class=HTMLResponse)
def home(request:Request):
    return templates.TemplateResponse(request=request,name="index.html",context={**status_data(),"cities":CITIES,"csrf":csrf(),"connections":{k:bool(secrets().get(k)) for k in ("apify_token","airtable_token")},"base":SETTINGS["airtable_base"],"table":SETTINGS["airtable_table"],"notice":request.query_params.get("notice","")})


@app.get("/api/status")
def status():
    return status_data()


@app.get('/api/coverage/{city_id}')
def coverage_report(city_id:str):
    if city_id not in {c['id'] for c in CITIES}:raise HTTPException(404,'Невідоме місто')
    with db.connect() as c:
        job=c.execute('SELECT * FROM jobs WHERE city_id=%s ORDER BY created_at DESC LIMIT 1',(city_id,)).fetchone()
        if not job:raise HTTPException(404,'Збір ще не запускався')
        passes=c.execute("SELECT id,key,kind,input->'_zone'->>'name' zone,status,result_count,cost_usd,reason,run_id FROM passes WHERE job_id=%s ORDER BY id",(job['id'],)).fetchall()
        totals=c.execute("SELECT count(*) candidates,count(*) FILTER(WHERE p.inspect_status='done') checked,count(*) FILTER(WHERE p.inspect_status<>'done') queued,count(*) FILTER(WHERE p.inspect_status='done' AND p.inspection->'qualification'->>'eligible'='true' AND p.inspection->'qualification'->>'priority' IN ('A','B')) ready,count(*) FILTER(WHERE (p.inspect_status='done' AND p.inspection->'qualification'->>'eligible'='true' AND p.inspection->'qualification'->>'priority'='C') OR (p.inspect_status<>'done' AND p.airtable_id IS NOT NULL)) review FROM places p JOIN job_places jp ON jp.place_id=p.id WHERE jp.job_id=%s",(job['id'],)).fetchone()
        lists=c.execute('SELECT url,status,reason,candidates FROM coverage_pages WHERE job_id=%s ORDER BY id',(job['id'],)).fetchall()
        calls=c.execute("SELECT cycle,calls FROM budgets WHERE service='airtable' ORDER BY cycle DESC LIMIT 1").fetchone()
        queue=c.execute("SELECT p.id,p.name,p.phone,p.address,p.inspect_status,CASE WHEN p.raw->>'_legacy_only'='true' THEN 'Потрібна ідентифікація старого запису' WHEN COALESCE(p.raw->>'_serp_complete','false')<>'true' THEN 'Не завершено пошук у Google' WHEN p.inspect_status='running' THEN 'Триває перевірка сайту' ELSE 'Очікує локальної перевірки сайту' END reason FROM places p JOIN job_places jp ON jp.place_id=p.id WHERE jp.job_id=%s AND p.inspect_status<>'done' ORDER BY p.name",(job['id'],)).fetchall()
    return {'generated_at':datetime.now(ZoneInfo('Europe/Madrid')).isoformat(),'city':job['city_name'],'job_status':job['status'],'reason':job['reason'],'complete':job['status']=='complete','totals':totals,'passes':passes,'public_lists':lists,'queued_candidates':queue,'apify_budget':db.get('apify_budget'),'airtable_api':calls,'limitation':'Завершення запланованих проходів не доводить, що в інтернеті знайдено кожен існуючий заклад.'}


@app.post("/jobs")
def submit_job(city_id:str=Form(...),mode:str=Form("start"),csrf_token:str=Form(...)):
    check_csrf(csrf_token)
    if city_id not in {c["id"] for c in CITIES} or mode not in {"start","continue","recheck"}:
        raise HTTPException(400,"Невідоме місто або дія")
    if os.environ.get("RESTORE_MODE","false") == "true":
        raise HTTPException(503,"Тестове відновлення: збір вимкнено")
    pipeline.create_job(city_id,mode,pilot=not db.get("pilot_approved",False))
    return RedirectResponse("/?notice=Збір+додано+в+чергу.+Прогрес+з’явиться+нижче",status_code=303)


@app.get("/connections",response_class=HTMLResponse)
def connections(request:Request):
    return templates.TemplateResponse(request=request,name="connections.html",context={"csrf":csrf(),"connected":{k:bool(secrets().get(k)) for k in ("apify_token","airtable_token")},"notice":request.query_params.get("notice","")})


@app.post("/connections")
def save_connections(apify_token:str=Form(""),airtable_token:str=Form(""),csrf_token:str=Form(...)):
    check_csrf(csrf_token)
    save_secrets({"apify_token":apify_token,"airtable_token":airtable_token})
    return RedirectResponse("/connections?notice=Збережено+локально.+Натисни+Перевірити+підключення",status_code=303)


@app.post("/connections/check")
def check_connections(csrf_token:str=Form(...)):
    check_csrf(csrf_token)
    Apify().budget()
    pipeline.refresh_crm(force=True)
    return RedirectResponse("/?notice=Apify+і+Airtable+перевірені.+Попередні+відмови+імпортовано",status_code=303)


class JobInput(BaseModel):
    city_id:str="gandia"
    mode:str="start"
    pilot:bool=True


@app.post("/internal/jobs",dependencies=[Depends(internal)])
def internal_job(data:JobInput):
    if data.mode not in {"start","continue","recheck"} or data.city_id not in {c["id"] for c in CITIES}:
        raise HTTPException(400,"Неприпустиме місто або дія")
    return {"job_id":pipeline.create_job(data.city_id,data.mode,data.pilot and not db.get("pilot_approved",False)),"dashboard":"http://localhost:5680/"}


@app.post("/internal/tick/{stage}",dependencies=[Depends(internal)])
def tick(stage:str,dry_run:bool=False):
    if stage not in {"discovery","enrichment","sync","maintenance"}:
        raise HTTPException(404,"unknown_stage")
    if dry_run:
        with db.connect() as c:
            queued=c.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','running')").fetchone()["n"]
        return {"read_only":True,"stage":stage,"queued_jobs":queued,"legacy_imported":db.get("legacy_imported",False),"apify_configured":bool(secrets().get("apify_token")),"airtable_configured":bool(secrets().get("airtable_token"))}
    try:
        result = getattr(pipeline,stage)()
        db.put("heartbeat:"+stage,{"at":datetime.now().astimezone().isoformat(),"result":result})
        return result
    except ServiceError:
        raise
    except Exception:
        db.event(stage,"Внутрішня помилка. Прогрес збережений; потрібна перевірка журналу контейнера","error")
        raise HTTPException(500,"Внутрішня помилка обробки; прогрес збережено") from None


@app.post("/internal/errors",dependencies=[Depends(internal)])
def workflow_error():
    db.event("n8n","Workflow n8n завершився помилкою. Деталі у Executions","error")
    return {"recorded":True}
