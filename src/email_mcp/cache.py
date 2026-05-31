import hashlib
import json
import time

from email_mcp import store

TTL_SECONDS = 60 * 60


def make_key(account, filters, include_sent, strip):
    material = json.dumps(
        {"a": account, "f": filters or {}, "s": include_sent, "t": strip},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def set(db_path, key, value, now=None):
    now = time.time() if now is None else now
    payload = json.dumps({"v": value, "ts": now}).encode()
    with store.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO cache(key, payload, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "created_at=excluded.created_at",
            (key, payload, str(now)),
        )


def get(db_path, key, now=None):
    now = time.time() if now is None else now
    with store.connect(db_path) as conn:
        row = conn.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    data = json.loads(row[0])
    if now - float(data["ts"]) > TTL_SECONDS:
        return None
    return data["v"]
