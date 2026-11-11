#!/usr/bin/env python3
"""Restore into an isolated disposable project; never start restored workflows."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid

ROOT=Path(__file__).resolve().parent.parent
PROJECT="cortex-leads-restore-check-"+uuid.uuid4().hex[:8]


def execute(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,**kwargs)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("archive",type=Path)
    args=parser.parse_args()
    archive=args.archive.resolve()
    key=ROOT/".secrets/recovery-key.agekey"
    if not key.exists():raise RuntimeError("Recovery key not found")
    parent=ROOT/".secrets"
    with tempfile.TemporaryDirectory(prefix="restore-check-",dir=parent) as tmp:
        work=Path(tmp)
        result=execute(["docker","run","--rm","-v",str(archive.parent)+":/archive:ro","-v",str(key.parent)+":/keys:ro","cortex-leads-backup:1.0.0","age","-d","-i","/keys/recovery-key.agekey","/archive/"+archive.name])
        tarpath=work/"snapshot.tar.gz"
        tarpath.write_bytes(result.stdout)
        tarpath.chmod(0o600)
        extracted=work/"snapshot"
        extracted.mkdir(mode=0o700)
        with tarfile.open(tarpath) as tar:
            members=[]
            for entry in tar.getmembers():
                target=(extracted/entry.name).resolve()
                if os.path.commonpath([str(extracted),str(target)])!=str(extracted) or entry.issym() or entry.islnk():
                    raise RuntimeError("Unsafe archive entry")
                members.append(entry)
            tar.extractall(extracted,members=members)
        manifest=json.loads((extracted/"manifest.json").read_text())
        for name,digest in manifest["sha256"].items():
            assert hashlib.sha256((extracted/name).read_bytes()).hexdigest()==digest,"Backup checksum mismatch"
        main_report={}
        if manifest.get('main_n8n'):
            assert manifest['main_n8n']['image']=='n8n-reels:latest','Unexpected main n8n image'
            with sqlite3.connect(extracted/'main-n8n/database.sqlite') as main_db:
                assert main_db.execute('PRAGMA quick_check').fetchone()[0]=='ok','Main n8n database integrity check failed'
                main_report['restored_main_n8n_workflows']=main_db.execute('SELECT count(*) FROM workflow_entity').fetchone()[0]
                main_report['restored_main_n8n_credentials']=main_db.execute('SELECT count(*) FROM credentials_entity').fetchone()[0]
            assert main_report['restored_main_n8n_workflows']==manifest['main_n8n']['workflows']
            # The extracted folder is disposable. No production volumes or networks are used.
            main_exported=execute(['docker','run','--rm','--pull=never','--network','none','--env-file',str(extracted/'main-project/.env'),'-v',str(extracted/'main-n8n')+':/home/node/.n8n','--entrypoint','n8n','n8n-reels:latest','export:credentials','--all','--decrypted']).stdout.decode()
            decoder=json.JSONDecoder()
            main_credentials=None
            for i,ch in enumerate(main_exported):
                if ch=='[':
                    try:
                        candidate,_=decoder.raw_decode(main_exported[i:])
                        if isinstance(candidate,list) and candidate and isinstance(candidate[0],dict) and 'data' in candidate[0]:main_credentials=candidate;break
                    except ValueError:pass
            assert main_credentials and len(main_credentials)==main_report['restored_main_n8n_credentials'],'Main n8n credentials could not be decrypted'
            main_report['main_n8n_credentials_decrypted']=True
        deployment=work/"deployment"
        deployment.mkdir()
        for name in ("compose.yaml","Dockerfile","requirements.txt"):
            shutil.copy2(ROOT/name,deployment/name)
        for name in ("scripts","config","app","workflows","backup"):
            shutil.copytree(ROOT/name,deployment/name,ignore=shutil.ignore_patterns("__pycache__"))
        state=deployment/".secrets"
        state.mkdir(mode=0o700)
        (state/"n8n-import").mkdir()
        (state/"rclone").mkdir()
        for name in ("runtime","backups"):(deployment/name).mkdir()
        values=dict(line.split("=",1) for line in (extracted/"deployment.env").read_text().splitlines() if "=" in line)
        values.update(N8N_PORT="5699",CONTROL_PORT="5700",RESTORE_MODE="true")
        env=deployment/".env"
        env.write_text("\n".join(k+"="+v for k,v in values.items())+"\n")
        env.chmod(0o600)
        base=["docker","compose","--project-directory",str(deployment),"-p",PROJECT]
        try:
            execute([*base,"up","-d","postgres"])
            import time
            for _ in range(30):
                ready=subprocess.run([*base,"exec","-T","postgres","pg_isready","-h","127.0.0.1","-U","cortex","-d","cortex"],capture_output=True)
                if not ready.returncode:break
                time.sleep(1)
            else:
                raise RuntimeError('Isolated PostgreSQL did not become ready')
            for database in ("cortex","n8n"):
                execute([*base,"exec","-T","postgres","pg_restore","-U","cortex","-d",database,"--no-owner","--no-acl"],input=(extracted/(database+".dump")).read_bytes())
            count=execute([*base,"exec","-T","postgres","psql","-U","cortex","-d","cortex","-Atc","SELECT count(*) FROM places"]).stdout.decode().strip()
            execute([*base,"run","--rm","--no-deps","--user","0:0","--entrypoint","sh","-v",str(extracted/"n8n")+":/restore-n8n:ro","n8n","-c","cp -R /restore-n8n/. /home/node/.n8n/ && chown -R 1000:1000 /home/node/.n8n"])
            # CLI only: importing database does not activate triggers. Decryption proves the n8n key is correct.
            exported=execute([*base,"run","--rm","--no-deps","n8n","export:credentials","--all","--decrypted"]).stdout.decode()
            decoder=json.JSONDecoder()
            decrypted=None
            for i,ch in enumerate(exported):
                if ch=="[":
                    try:
                        candidate,_=decoder.raw_decode(exported[i:])
                        if isinstance(candidate,list) and candidate and isinstance(candidate[0],dict) and "data" in candidate[0]:decrypted=candidate;break
                    except ValueError:pass
            assert decrypted and any(c.get("data",{}).get("value")=="Bearer "+values["INTERNAL_TOKEN"] for c in decrypted),"n8n credentials could not be decrypted"
            report={"archive":archive.name,"checksums":True,"postgres_restore":True,"n8n_credentials_decrypted":True,"restored_place_count":int(count),"restored_workflows_executed":False,"isolated_project":PROJECT,**main_report}
            (ROOT/"runtime/restore-check.json").write_text(json.dumps(report,indent=2))
            print(json.dumps(report,indent=2))
        finally:
            # Only this script's explicitly disposable test project; never use down -v for production.
            assert PROJECT.startswith("cortex-leads-restore-check-") and len(PROJECT)==len("cortex-leads-restore-check-")+8
            execute([*base,"down","--volumes","--remove-orphans"])
