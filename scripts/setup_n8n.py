#!/usr/bin/env python3
"""Import Cortex into the existing main n8n; never create another owner."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent.parent
MAIN_CONTAINER = "n8n"
MAIN_URL = "http://localhost:5678/"


def run(*args, **kwargs):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError("Command failed: " + args[0] + ". Inspect local logs; no credentials were printed.")
    return result.stdout


def command(*args):
    run("docker", "exec", MAIN_CONTAINER, "n8n", *args)
    print(args[0] + ": OK")


def owner_id():
    script = """
const sqlite3 = require('/usr/local/lib/node_modules/n8n/node_modules/sqlite3');
const db = new sqlite3.Database('/home/node/.n8n/database.sqlite', sqlite3.OPEN_READONLY);
db.all("SELECT id FROM user WHERE roleSlug='global:owner'", (err, rows) => {
  if (err || rows.length !== 1) { process.exitCode = 1; }
  else { console.log(rows[0].id); }
  db.close();
});
"""
    return run("docker", "exec", "-i", MAIN_CONTAINER, "node", "-", input=script.encode()).decode().strip()


def already_imported():
    """Avoid rewriting existing Cortex credentials and workflow versions on activation."""
    expected = [json.loads(path.read_text()) for path in (ROOT / 'workflows').glob('*.json')]
    script = "const expected = " + json.dumps(expected) + ";\n" + r"""
const sqlite3 = require('/usr/local/lib/node_modules/n8n/node_modules/sqlite3');
const {isDeepStrictEqual} = require('util');
const db = new sqlite3.Database('/home/node/.n8n/database.sqlite', sqlite3.OPEN_READONLY);
db.all('SELECT id,nodes,connections,settings FROM workflow_entity', (err, rows) => {
  if (err) {process.exitCode=1; db.close(); return;}
  db.all('SELECT id FROM credentials_entity', (error, credentials) => {
    if (error) {process.exitCode=1; db.close(); return;}
    const installed = expected.every(definition => {
      const row=rows.find(item=>item.id===definition.id);
      return row && ['nodes','connections','settings'].every(key=>isDeepStrictEqual(JSON.parse(row[key]),definition[key]))
        && definition.nodes.every(node=>Object.values(node.credentials||{}).every(ref=>credentials.some(c=>c.id===ref.id)));
    });
    console.log(JSON.stringify(installed)); db.close();
  });
});
"""
    return json.loads(run('docker','exec','-i',MAIN_CONTAINER,'node','-',input=script.encode()).decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    if already_imported():
        print('Matching Cortex workflows and credentials already exist; preserving them.')
    else:
        user = owner_id()
        target = "/tmp/cortex-import-" + uuid.uuid4().hex
        run("docker", "exec", MAIN_CONTAINER, "mkdir", "-m", "700", target)
        run("docker", "exec", "-i", MAIN_CONTAINER, "sh", "-c", "umask 077; cat > " + target + "/internal.json", input=(ROOT / ".secrets/n8n-import/internal.json").read_bytes())
        run("docker", "cp", str(ROOT / "workflows"), MAIN_CONTAINER + ":" + target + "/workflows")
        try:
            command("import:credentials", "--input=" + target + "/internal.json", "--userId=" + user)
            command("import:workflow", "--separate", "--input=" + target + "/workflows", "--userId=" + user)
        finally:
            # This invocation created this private directory solely for its import files.
            run("docker", "exec", "--user", "0:0", MAIN_CONTAINER, "rm", "-rf", target)
    if args.activate:
        for file in sorted((ROOT / "workflows").glob("*.json")):
            workflow = json.loads(file.read_text())
            if workflow["id"] != "CortexErrorFlow01":
                command("publish:workflow", "--id=" + workflow["id"])
        run("docker", "restart", MAIN_CONTAINER)
    print("Cortex imported into the existing main n8n: " + MAIN_URL)
    print("Existing owner, Reels workflows and their credentials were preserved.")
