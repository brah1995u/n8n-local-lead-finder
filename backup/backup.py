"""Encrypted snapshots. Remote upload must succeed before success or retention runs."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone

ROOT=Path("/backups")
STATE=Path("/runtime")
SECRETS=Path("/snapshot/secrets")


def snapshot_main_n8n(work,source=Path('/snapshot/main-n8n'),original=Path('/snapshot/main-project')):
    """Copy the main instance using SQLite's online backup, including its WAL."""
    if not (source/'database.sqlite').is_file():
        raise RuntimeError('Main n8n database is not mounted')
    target=work/'main-n8n'
    shutil.copytree(source,target,ignore=shutil.ignore_patterns('database.sqlite','database.sqlite-wal','database.sqlite-shm','crash.journal','n8nEventLog*'))
    with closing(sqlite3.connect('file:'+str(source/'database.sqlite')+'?mode=ro',uri=True,timeout=30)) as live:
        with closing(sqlite3.connect(target/'database.sqlite')) as snapshot:
            live.backup(snapshot)
            # The archive must be self-contained, without SQLite WAL sidecars.
            snapshot.execute('PRAGMA journal_mode=DELETE')
            if snapshot.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise RuntimeError('Main n8n snapshot integrity check failed')
            counts={'workflows':snapshot.execute('SELECT count(*) FROM workflow_entity').fetchone()[0],
                    'credentials':snapshot.execute('SELECT count(*) FROM credentials_entity').fetchone()[0]}
    project=work/'main-project'
    project.mkdir(mode=0o700)
    for name in ('.env','docker-compose.yml','Dockerfile','README.md','build_workflow.py','import.sh'):
        shutil.copy2(original/name,project/name)
    for name in ('workflows','fonts'):
        path=original/'files'/name
        if path.is_dir():
            shutil.copytree(path,project/'files'/name)
    return counts


def run(args,**kwargs):
    if "stdout" in kwargs:
        return subprocess.run(args,check=True,stderr=subprocess.PIPE,**kwargs)
    return subprocess.run(args,check=True,capture_output=True,**kwargs)


def status(value,message="",**extra):
    target=STATE/"backup-status.json"
    temp=target.with_suffix(".tmp")
    now=datetime.now(timezone.utc).isoformat()
    previous=json.loads(target.read_text()) if target.exists() else {}
    last=now if value=='Копія в Google Drive перевірена' else previous.get('last_success_at') or (previous.get('at') if previous.get('status')=='Копія в Google Drive перевірена' else None)
    temp.write_text(json.dumps({"status":value,"message":message,"at":now,"last_success_at":last,**extra}))
    temp.replace(target)


def backup(upload=True):
    # Consolidation adds the existing Reels account and credentials to the payload.
    # Uploading that expanded payload requires the user's explicit authorization.
    upload=upload and os.environ.get('ALLOW_MAIN_N8N_DRIVE_UPLOAD','false').lower()=='true'
    recipient=SECRETS/"backup-recipient.txt"
    config=Path("/config/rclone/rclone.conf")
    if not recipient.exists() or (upload and not config.exists()):
        status("Не підключено","Потрібні ключ шифрування та авторизація Google Drive")
        return False
    ROOT.mkdir(exist_ok=True)
    now=datetime.now(timezone.utc)
    stamp=now.strftime("%Y-%m-%dT%H-%M-%SZ")
    with tempfile.TemporaryDirectory() as temp:
        work=Path(temp)
        for database in ("cortex","n8n"):
            with (work/(database+".dump")).open("wb") as out:
                run(["pg_dump","-Fc","--no-owner","--no-acl",database],stdout=out)
        main_counts=snapshot_main_n8n(work)
        # Store deployment secrets and n8n user folder only inside the encrypted stream.
        checksums={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in work.glob('*.dump')}
        checksums['main-n8n/database.sqlite']=hashlib.sha256((work/'main-n8n/database.sqlite').read_bytes()).hexdigest()
        (work/"manifest.json").write_text(json.dumps({"version":2,"created_at":now.isoformat(),"databases":["cortex","n8n"],"n8n_image":"docker.n8n.io/n8nio/n8n:2.44.7","main_n8n":{"image":"n8n-reels:latest","url":"http://localhost:5678/",**main_counts},"sha256":checksums}))
        tarpath=work/"snapshot.tar.gz"
        with tarfile.open(tarpath,"w:gz") as archive:
            for name in ("cortex.dump","n8n.dump","manifest.json"):
                archive.add(work/name,arcname=name)
            archive.add("/snapshot/n8n",arcname="n8n")
            archive.add(work/'main-n8n',arcname='main-n8n')
            archive.add(work/'main-project',arcname='main-project')
            archive.add("/snapshot/deployment.env",arcname="deployment.env")
            archive.add(SECRETS/"connections.json",arcname="secrets/connections.json") if (SECRETS/"connections.json").exists() else None
            archive.add(SECRETS/"n8n-owner.json",arcname="secrets/n8n-owner.json") if (SECRETS/"n8n-owner.json").exists() else None
            archive.add(SECRETS/"legacy-n8n-owner.json",arcname="secrets/legacy-n8n-owner.json") if (SECRETS/"legacy-n8n-owner.json").exists() else None
            archive.add(SECRETS/"rclone",arcname="secrets/rclone")
        encrypted=ROOT/("cortex-daily-"+now.strftime("%Y-%m-%d")+".tar.gz.age")
        pending=ROOT/("cortex-"+stamp+".partial")
        run(["age","-r",recipient.read_text().strip(),"-o",str(pending),str(tarpath)])
        pending.chmod(0o600)
        pending.replace(encrypted)
        if not upload:
            (STATE/"backup-request").unlink(missing_ok=True)
            status("Локальна копія зашифрована","Завантаження повної копії Reels і Cortex у Drive очікує підтвердження",file=encrypted.name,bytes=encrypted.stat().st_size)
            return str(encrypted)
        common=["--config",str(config)]
        remote="cortex-drive:"+encrypted.name
        run(["rclone","copyto",str(encrypted),remote,*common])
        listing=json.loads(run(["rclone","lsjson",remote,*common]).stdout)
        entry=listing[0] if isinstance(listing,list) else listing
        if entry.get("Size") != encrypted.stat().st_size:
            raise RuntimeError("Remote size verification failed")
        weekly="cortex-weekly-"+now.strftime("%G-W%V")+".tar.gz.age"
        run(["rclone","copyto",str(encrypted),"cortex-drive:"+weekly,*common])
        # Only archives generated by this application in its dedicated Drive folder.
        files=json.loads(run(["rclone","lsjson","cortex-drive:",*common]).stdout)
        for period,keep in (("daily",7),("weekly",4)):
            names=sorted([f["Name"] for f in files if f["Name"].startswith("cortex-"+period+"-") and f["Name"].endswith(".tar.gz.age")],reverse=True)
            for old in names[keep:]:
                run(["rclone","deletefile","cortex-drive:"+old,*common])
        local=sorted(ROOT.glob("cortex-daily-*.tar.gz.age"),reverse=True)
        for old in local[7:]:
            old.unlink()
        (STATE/"backup-request").unlink(missing_ok=True)
        status("Копія в Google Drive перевірена","Архів зашифрований; віддалений розмір підтверджено",file=encrypted.name,bytes=encrypted.stat().st_size)
        return True


if __name__=="__main__":
    STATE.mkdir(exist_ok=True)
    while True:
        last=STATE/"backup-status.json"
        previous=json.loads(last.read_text()) if last.exists() else {}
        success=previous.get("status") in ("Копія в Google Drive перевірена","Локальна копія зашифрована")
        stale=not success or (datetime.now(timezone.utc)-datetime.fromisoformat(previous["at"])).total_seconds()>86400
        if stale or (STATE/"backup-request").exists():
            try:
                backup()
            except Exception:
                status("Помилка резервування","Попередні архіви збережено. Перевір авторизацію Drive та місце; повтор через 10 хвилин")
        time.sleep(600)
