"""AMURA Club foundation. No automatic awards or storefront changes yet."""
import json
import time
import uuid

DEFAULT = {"target": 10, "minimum_kzt": 20000, "gift_cost_kzt": 3500}

def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULT):
        raise ValueError("Invalid Club settings")
    for key, low, high in (("target", 1, 100), ("minimum_kzt", 1, 10000000),
                           ("gift_cost_kzt", 0, 10000000)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError("Invalid " + key)
    return dict(data)

def schema(d):
    d.run("CREATE TABLE IF NOT EXISTS club_config_history (id TEXT PRIMARY KEY, previous_id TEXT, data TEXT NOT NULL, actor TEXT NOT NULL, at DOUBLE PRECISION NOT NULL)")
    d.run("CREATE TABLE IF NOT EXISTS club_config_head (id INTEGER PRIMARY KEY, version TEXT NOT NULL)")
    d.run("INSERT INTO club_config_head (id, version) VALUES (1, '') ON CONFLICT (id) DO NOTHING")

def settings(d):
    schema(d)
    row = d.run("SELECT h.version, c.data FROM club_config_head h LEFT JOIN club_config_history c ON c.id=h.version WHERE h.id=1", one=True)
    return {"version": row[0], **(json.loads(row[1]) if row[1] else DEFAULT)}

def save(d, data, actor, expected_version):
    data = validate(data)
    if not actor:
        raise ValueError("Actor required")
    current = settings(d)
    if current["version"] != expected_version:
        raise ValueError("Settings changed; reload")
    version = uuid.uuid4().hex
    d.run("INSERT INTO club_config_history (id, previous_id, data, actor, at) VALUES (%s,%s,%s,%s,%s)",
          (version, expected_version, json.dumps(data, sort_keys=True), actor, time.time()))
    d.run("UPDATE club_config_head SET version=%s WHERE id=1 AND version=%s", (version, expected_version))
    if d.cur.rowcount != 1:
        raise ValueError("Settings changed; reload")
    return {"version": version, **data}

def restore(d, version, actor, expected_version):
    schema(d)
    row = d.run("SELECT data FROM club_config_history WHERE id=%s", (version,), one=True) if version else None
    if version and not row:
        raise ValueError("Unknown version")
    return save(d, json.loads(row[0]) if row else DEFAULT, actor, expected_version)
