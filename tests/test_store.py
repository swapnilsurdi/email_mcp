import sqlite3
from email_mcp import store


def test_init_db_creates_tables(db_path):
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "cache", "send_ledger"} <= names


def test_wal_mode_enabled(db_path):
    conn = sqlite3.connect(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_settings_roundtrip(db_path):
    store.set_setting(db_path, "default_account", "icloud-personal")
    assert store.get_setting(db_path, "default_account") == "icloud-personal"
    assert store.get_setting(db_path, "missing") is None
