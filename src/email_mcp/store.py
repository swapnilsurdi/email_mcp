import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS cache (
  key        TEXT PRIMARY KEY,
  payload    BLOB,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS send_ledger (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account         TEXT NOT NULL,
  key_kind        TEXT NOT NULL,
  key_hash        TEXT NOT NULL,
  recipients      TEXT NOT NULL,
  subject_excerpt TEXT,
  status          TEXT NOT NULL,
  tags            TEXT,
  sent_at         TEXT NOT NULL,
  message_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_lookup
  ON send_ledger(key_kind, key_hash, sent_at);
"""


@contextmanager
def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def set_setting(db_path, key, value):
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_setting(db_path, key):
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
