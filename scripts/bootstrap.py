#!/usr/bin/env python3
"""Generate local secrets once. Safe to re-run; never overwrite existing keys."""
import json
import os
from pathlib import Path
import secrets

ROOT=Path(__file__).resolve().parent.parent
state=ROOT/".secrets"
state.mkdir(mode=0o700,exist_ok=True)
for name in ("n8n-import","rclone","bin"):
    (state/name).mkdir(mode=0o700,exist_ok=True)
for name in ("backups","runtime","workflows"):
    (ROOT/name).mkdir(exist_ok=True)
env=ROOT/".env"
if not env.exists():
    password=secrets.token_hex(24)
    internal=secrets.token_hex(32)
    encryption=secrets.token_hex(32)
    env.write_text(f"POSTGRES_PASSWORD={password}\nN8N_ENCRYPTION_KEY={encryption}\nINTERNAL_TOKEN={internal}\nDATABASE_URL=postgresql://cortex:{password}@postgres:5432/cortex\nN8N_PORT=5679\nCONTROL_PORT=5680\nRESTORE_MODE=false\n")
    env.chmod(0o600)
values=dict(line.split("=",1) for line in env.read_text().splitlines() if "=" in line)
credential=[{"id":"cortexInternalAuth","name":"Cortex Internal API","type":"httpHeaderAuth","data":{"name":"Authorization","value":"Bearer "+values["INTERNAL_TOKEN"]}}]
path=state/"n8n-import/internal.json"
path.write_text(json.dumps(credential))
path.chmod(0o600)
print("Local configuration ready. No existing secrets overwritten.")
