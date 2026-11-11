#!/usr/bin/env python3
"""Generate one recovery key; keep it out of snapshots and Git."""
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
state=ROOT/".secrets"
key=state/"recovery-key.agekey"
base=["docker","run","--rm","-v",str(state)+":/keys","cortex-leads-backup:1.0.0"]
if not key.exists():
    subprocess.run([*base,"age-keygen","-o","/keys/recovery-key.agekey"],check=True,capture_output=True)
key.chmod(0o600)
recipient=subprocess.check_output([*base,"age-keygen","-y","/keys/recovery-key.agekey"],text=True).strip()
assert recipient.startswith("age1")
(state/"backup-recipient.txt").write_text(recipient+"\n")
print("Recovery key created/preserved: .secrets/recovery-key.agekey. Save it in your password manager.")
