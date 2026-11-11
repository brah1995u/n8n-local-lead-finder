#!/usr/bin/env python3
"""Authorize Drive through packaged rclone. No OAuth token is displayed."""
import re
import configparser
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
FOLDER_NAME="Cortex Leads — Encrypted Backups"

if __name__=="__main__":
    config=ROOT/".secrets/rclone/rclone.conf"
    # drive.file allows only files created/opened with rclone, rather than the whole Drive.
    if "--finish" not in sys.argv:
        command="socat TCP-LISTEN:53683,bind=0.0.0.0,fork TCP:127.0.0.1:53682 >/dev/null 2>&1 & cortex_proxy_pid=$!; trap 'kill $cortex_proxy_pid 2>/dev/null || true' EXIT; rclone config create cortex-drive drive scope drive.file config_is_local true config_auth_no_browser true --config /auth/rclone.conf"
        process=subprocess.Popen(["docker","compose","run","--rm","--no-deps","-p","127.0.0.1:53682:53683","-v",str(config.parent)+":/auth","backup","bash","-c",command],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for line in process.stdout:
            match=re.search(r'http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_\-]+',line)
            if match:print("Authorize locally: "+match.group(0),flush=True)
            elif "Waiting for code" in line:print("Waiting for browser authorization",flush=True)
        if process.wait():raise RuntimeError("Google Drive authorization did not complete; no tokens displayed")
    config.chmod(0o600)
    import json
    def remote(*args):
        result=subprocess.run(["docker","compose","run","--rm","--no-deps","-v",str(config.parent)+":/auth","backup","rclone",*args,"--config","/auth/rclone.conf"],cwd=ROOT,capture_output=True,text=True,check=True)
        return result.stdout
    parsed=configparser.ConfigParser(interpolation=None)
    parsed.read(config)
    existing_root=parsed["cortex-drive"].get("root_folder_id")
    if existing_root:
        folder={"ID":existing_root}
    else:
        remote("mkdir","cortex-drive:"+FOLDER_NAME)
        folders=json.loads(remote("lsjson","cortex-drive:","--dirs-only"))
        folder=next(x for x in folders if x["Name"]==FOLDER_NAME)
    # Config update may start a second interactive OAuth flow. Set only the local
    # root folder setting while preserving the authorized narrow scope and token.
    parsed["cortex-drive"]["root_folder_id"]=folder["ID"]
    with config.open("w") as stream:parsed.write(stream)
    config.chmod(0o600)
    (ROOT/".secrets/drive-folder.json").write_text(json.dumps({"id":folder["ID"],"name":FOLDER_NAME}))
    (ROOT/"runtime/backup-request").touch()
    print("Google Drive configured. Backup will verify its first encrypted upload.")
