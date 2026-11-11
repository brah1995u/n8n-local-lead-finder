import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("STATE_DIR", "/state"))
SETTINGS = json.loads((ROOT / "config/settings.json").read_text())
CITIES = json.loads((ROOT / "config/cities.json").read_text())


def secrets():
    path = STATE / "connections.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_secrets(values):
    STATE.mkdir(exist_ok=True, parents=True)
    path = STATE / "connections.json"
    data = secrets()
    data.update({k: v.strip() for k, v in values.items() if v and v.strip()})
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.chmod(0o600)
    temp.replace(path)


def city(city_id):
    return next(c for c in CITIES if c["id"] == city_id)
