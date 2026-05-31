# Email MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local MCP server giving AI agents safe, multi-account email access over IMAP/SMTP (iCloud + Gmail via app-specific passwords), with cross-process caching, cross-folder search, and idempotent sends.

**Architecture:** A FastMCP server exposes 8 tools over a single generic IMAP/SMTP account abstraction (iCloud/Gmail differ only by config rows). All shared state — 60-minute result cache, send-idempotency ledger, default-account selection — lives in one SQLite (WAL) database so concurrent agents stay consistent. Connect-per-call IMAP/SMTP (no pooling). Secrets live in the macOS Keychain only. Reuses the proven `BODY.PEEK[]` + `readonly` fetch from the existing `mailconnector` so mail is never marked read.

**Tech Stack:** Python 3.12, `mcp` (FastMCP), `keyring` (macOS Keychain), stdlib `imaplib`/`smtplib`/`email`, `beautifulsoup4` (HTML→text), `PyYAML`, `pytest`.

**Reference spec:** `docs/2026-05-28-email-mcp-design.md`

> **CRITICAL naming note:** The repo folder is `mcps/email/`, but the Python package is **`email_mcp`** — a package literally named `email` would shadow the stdlib `email` module that IMAP parsing depends on. Never name the package `email`.

---

## File Structure

```
mcps/email/
  pyproject.toml                  # package metadata + deps + pytest config
  config/accounts.yml             # NON-SECRET account list (created in Task 2)
  src/email_mcp/
    __init__.py
    store.py                      # SQLite WAL: connection, schema, settings helpers
    accounts.py                   # accounts.yml load + Keychain password resolution
    setup_cli.py                  # `python -m email_mcp.setup_cli <account>` -> Keychain
    textutil.py                   # plain-text extraction, non-ASCII strip, token sizing
    cache.py                      # 60-min TTL cache over store
    ledger.py                     # send idempotency (3 keys, status, block logic)
    providers/
      __init__.py
      imap_account.py             # connect, list_folders, fetch, mark, move
      smtp_send.py                # SMTP send (STARTTLS + requireTLS)
    server.py                     # FastMCP entrypoint; registers the 8 tools
  tests/
    conftest.py                   # fixtures: temp DB, fake IMAP/SMTP doubles
    test_store.py
    test_accounts.py
    test_textutil.py
    test_cache.py
    test_ledger.py
    test_imap_account.py
    test_smtp_send.py
    test_server_tools.py
    live_smoke_test.py            # env-gated, manual only
```

Each file has one responsibility. Tasks below are ordered so every task leaves the suite green.

---

## Task 0: Project scaffold

**Files:**
- Create: `pyproject.toml`, `src/email_mcp/__init__.py`, `src/email_mcp/providers/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "email-mcp"
version = "0.1.0"
description = "Local MCP server for multi-account IMAP/SMTP email"
requires-python = ">=3.12"
dependencies = [
    "mcp>=1.2.0",
    "keyring>=25.0.0",
    "beautifulsoup4>=4.12.0",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/email_mcp"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Create empty package files**

`src/email_mcp/__init__.py`:
```python
"""Local MCP server for multi-account IMAP/SMTP email."""
```
`src/email_mcp/providers/__init__.py`:
```python
```

- [ ] **Step 3: Create `tests/conftest.py` with the temp-DB fixture**

```python
import pytest

from email_mcp import store


@pytest.fixture
def db_path(tmp_path):
    """A fresh, schema-initialized SQLite DB for each test."""
    path = tmp_path / "state.db"
    store.init_db(str(path))
    return str(path)
```

- [ ] **Step 4: Create the venv and install**

Run: `cd /path/to/email-mcp && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
Expected: installs without error; `email-mcp` shown as editable install.

- [ ] **Step 5: Commit**

```bash
cd /path/to/email-mcp
git init -q 2>/dev/null; git add pyproject.toml src tests
git commit -m "chore: scaffold email-mcp package"
```

---

## Task 1: SQLite store (WAL) — schema + settings

**Files:**
- Create: `src/email_mcp/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError: init_db`.

- [ ] **Step 3: Write `store.py`**

```python
# src/email_mcp/store.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/store.py tests/test_store.py tests/conftest.py
git commit -m "feat: SQLite WAL store with settings helpers"
```

---

## Task 2: Accounts config + Keychain resolution + setup CLI

**Files:**
- Create: `src/email_mcp/accounts.py`, `src/email_mcp/setup_cli.py`, `config/accounts.yml`
- Test: `tests/test_accounts.py`

- [ ] **Step 1: Write the failing test** (Keychain is monkeypatched — no real secrets in tests)

```python
# tests/test_accounts.py
import pytest
from email_mcp import accounts

ACCOUNTS_YML = """
accounts:
  - name: icloud-personal
    email: me@icloud.com
    imap_host: imap.mail.me.com
    imap_port: 993
    smtp_host: smtp.mail.me.com
    smtp_port: 587
    default: true
  - name: gmail-personal
    email: me@gmail.com
    imap_host: imap.gmail.com
    imap_port: 993
    smtp_host: smtp.gmail.com
    smtp_port: 587
"""


@pytest.fixture
def accounts_file(tmp_path):
    p = tmp_path / "accounts.yml"
    p.write_text(ACCOUNTS_YML)
    return str(p)


def test_list_accounts_reads_yaml(accounts_file):
    accs = accounts.load_accounts(accounts_file)
    names = [a["name"] for a in accs]
    assert names == ["icloud-personal", "gmail-personal"]


def test_yaml_default_used_when_no_db_default(accounts_file, db_path):
    name = accounts.resolve_default(accounts_file, db_path)
    assert name == "icloud-personal"


def test_db_default_overrides_yaml(accounts_file, db_path):
    accounts.set_default(accounts_file, db_path, "gmail-personal")
    assert accounts.resolve_default(accounts_file, db_path) == "gmail-personal"


def test_set_default_rejects_unknown(accounts_file, db_path):
    with pytest.raises(ValueError):
        accounts.set_default(accounts_file, db_path, "nope")


def test_get_account_resolves_password_from_keychain(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password",
                        lambda service, user: "app-pw" if user == "icloud-personal" else None)
    acc = accounts.get_account(accounts_file, "icloud-personal")
    assert acc["password"] == "app-pw"
    assert acc["imap_host"] == "imap.mail.me.com"


def test_get_account_missing_password_raises(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password", lambda s, u: None)
    with pytest.raises(RuntimeError, match="No password in Keychain"):
        accounts.get_account(accounts_file, "icloud-personal")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: FAIL — module/attrs missing.

- [ ] **Step 3: Write `accounts.py`**

```python
# src/email_mcp/accounts.py
import keyring
import yaml

from email_mcp import store

KEYRING_SERVICE = "email-mcp"


def load_accounts(accounts_file):
    with open(accounts_file) as f:
        data = yaml.safe_load(f) or {}
    return data.get("accounts", [])


def _find(accounts_file, name):
    for a in load_accounts(accounts_file):
        if a["name"] == name:
            return a
    return None


def resolve_default(accounts_file, db_path):
    db_default = store.get_setting(db_path, "default_account")
    accs = load_accounts(accounts_file)
    valid = {a["name"] for a in accs}
    if db_default in valid:
        return db_default
    for a in accs:
        if a.get("default"):
            return a["name"]
    return accs[0]["name"] if accs else None


def set_default(accounts_file, db_path, name):
    if _find(accounts_file, name) is None:
        raise ValueError(f"Unknown account: {name}")
    store.set_setting(db_path, "default_account", name)


def get_account(accounts_file, name):
    acc = _find(accounts_file, name)
    if acc is None:
        raise ValueError(f"Unknown account: {name}")
    pw = keyring.get_password(KEYRING_SERVICE, name)
    if not pw:
        raise RuntimeError(f"No password in Keychain for account '{name}'. "
                           f"Run: python -m email_mcp.setup_cli {name}")
    return {**acc, "password": pw}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Write `setup_cli.py` (manual credential entry; not unit-tested)**

```python
# src/email_mcp/setup_cli.py
import getpass
import sys

import keyring

from email_mcp.accounts import KEYRING_SERVICE


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m email_mcp.setup_cli <account-name>")
        sys.exit(2)
    name = sys.argv[1]
    pw = getpass.getpass(f"App-specific password for '{name}': ")
    if not pw:
        print("Empty password, aborting.")
        sys.exit(1)
    keyring.set_password(KEYRING_SERVICE, name, pw)
    print(f"Stored password for '{name}' in Keychain (service '{KEYRING_SERVICE}').")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Create `config/accounts.yml` (edit with real account names/hosts later)**

```yaml
accounts:
  - name: icloud-personal
    email: you@icloud.com
    imap_host: imap.mail.me.com
    imap_port: 993
    smtp_host: smtp.mail.me.com
    smtp_port: 587
    default: true
  # - name: gmail-personal
  #   email: you@gmail.com
  #   imap_host: imap.gmail.com
  #   imap_port: 993
  #   smtp_host: smtp.gmail.com
  #   smtp_port: 587
```

- [ ] **Step 7: Commit**

```bash
git add src/email_mcp/accounts.py src/email_mcp/setup_cli.py config/accounts.yml tests/test_accounts.py
git commit -m "feat: accounts config + Keychain resolution + setup CLI"
```

---

## Task 3: Text utilities — extraction, non-ASCII strip, token sizing

**Files:**
- Create: `src/email_mcp/textutil.py`
- Test: `tests/test_textutil.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_textutil.py
import email
from email_mcp import textutil


def _msg(plain=None, html=None):
    if plain and html:
        m = email.message.EmailMessage()
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
        return email.message_from_bytes(bytes(m))
    m = email.message.EmailMessage()
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain or "")
    return email.message_from_bytes(bytes(m))


def test_prefers_plain_text():
    assert "hello plain" in textutil.extract_text(_msg(plain="hello plain", html="<p>hi html</p>"))


def test_html_fallback_strips_tags():
    out = textutil.extract_text(_msg(html="<p>Click <a href='x'>here</a></p>"))
    assert "Click" in out and "here" in out and "<" not in out


def test_strip_non_ascii():
    assert textutil.strip_non_ascii("café — déjà ✓ vu") == "caf  dj  vu"


def test_estimate_tokens():
    assert textutil.estimate_tokens("a" * 400) == 100  # ~4 chars/token


def test_truncate_to_token_budget_trims_bodies_not_count():
    emails = [{"body": "x" * 4000} for _ in range(5)]  # ~1000 tokens each
    out, truncated = textutil.truncate_to_budget(emails, max_tokens=1000)
    assert len(out) == 5            # count preserved
    assert truncated is True
    assert sum(textutil.estimate_tokens(e["body"]) for e in out) <= 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_textutil.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `textutil.py`**

```python
# src/email_mcp/textutil.py
from bs4 import BeautifulSoup

CHARS_PER_TOKEN = 4


def extract_text(msg):
    """Best-effort plain text: prefer text/plain, fall back to HTML->text."""
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain is None:
                plain = _decode(part)
            elif ctype == "text/html" and html is None:
                html = _decode(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _decode(msg)
        else:
            plain = _decode(msg)
    if plain:
        return plain
    if html:
        return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    return ""


def _decode(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def strip_non_ascii(text):
    return "".join(c if ord(c) < 128 else " " for c in text)


def estimate_tokens(text):
    return len(text) // CHARS_PER_TOKEN


def truncate_to_budget(emails, max_tokens):
    """Trim email bodies so the total stays within max_tokens. Preserve count."""
    total = sum(estimate_tokens(e.get("body", "")) for e in emails)
    if total <= max_tokens:
        return emails, False
    n = max(len(emails), 1)
    per = max(max_tokens // n, 1)
    out = []
    for e in emails:
        b = e.get("body", "")
        if estimate_tokens(b) > per:
            b = b[: per * CHARS_PER_TOKEN]
        out.append({**e, "body": b})
    return out, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_textutil.py -v`
Expected: PASS (5 tests). (Note: `strip_non_ascii` replaces each non-ASCII char with a space, so "café" -> "caf ".)

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/textutil.py tests/test_textutil.py
git commit -m "feat: text extraction, non-ASCII strip, token sizing/truncation"
```

---

## Task 4: Cache with 60-minute TTL

**Files:**
- Create: `src/email_mcp/cache.py`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing test** (time is injected so tests don't sleep)

```python
# tests/test_cache.py
from email_mcp import cache


def test_make_key_is_stable_and_order_independent():
    k1 = cache.make_key("acct", {"b": 2, "a": 1}, include_sent=False, strip=False)
    k2 = cache.make_key("acct", {"a": 1, "b": 2}, include_sent=False, strip=False)
    assert k1 == k2


def test_set_then_get_within_ttl(db_path):
    cache.set(db_path, "k1", {"emails": [1, 2, 3]}, now=1000.0)
    assert cache.get(db_path, "k1", now=1000.0 + 59 * 60) == {"emails": [1, 2, 3]}


def test_get_returns_none_after_ttl(db_path):
    cache.set(db_path, "k1", {"x": 1}, now=1000.0)
    assert cache.get(db_path, "k1", now=1000.0 + 61 * 60) is None


def test_get_missing_returns_none(db_path):
    assert cache.get(db_path, "nope", now=1000.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cache.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `cache.py`**

```python
# src/email_mcp/cache.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cache.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/cache.py tests/test_cache.py
git commit -m "feat: 60-minute TTL cache over SQLite"
```

---

## Task 5: Send-idempotency ledger

**Files:**
- Create: `src/email_mcp/ledger.py`
- Test: `tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger.py
from email_mcp import ledger

REC = ["a@x.com", "b@y.com"]


def test_compute_keys_are_three_and_recipient_order_independent():
    k1 = ledger.compute_keys(REC, "Subj", "Body")
    k2 = ledger.compute_keys(list(reversed(REC)), "Subj", "Body")
    assert set(k1) == {"recipient", "recipient_subject", "recipient_body"}
    assert k1 == k2


def test_no_block_when_empty(db_path):
    assert ledger.check_block(db_path, REC, "S", "B", now=1000.0) is None


def test_recipient_only_is_hard_block_even_different_content(db_path):
    ledger.record_queued(db_path, "acct", REC, "S1", "B1", tags={"t": 1}, now=1000.0)
    block = ledger.check_block(db_path, REC, "S2-different", "B2-different", now=1000.0 + 60)
    assert block is not None
    assert "recipient" in block["matched"]
    assert block["prior"]["status"] == "queued"
    assert block["prior"]["tags"] == {"t": 1}


def test_block_expires_after_10_min(db_path):
    ledger.record_queued(db_path, "acct", REC, "S", "B", tags=None, now=1000.0)
    assert ledger.check_block(db_path, REC, "S", "B", now=1000.0 + 11 * 60) is None


def test_failed_send_still_blocks(db_path):
    ids = ledger.record_queued(db_path, "acct", REC, "S", "B", tags=None, now=1000.0)
    ledger.mark_failed(db_path, ids, now=1000.0)
    block = ledger.check_block(db_path, REC, "S", "B", now=1000.0 + 60)
    assert block is not None
    assert block["prior"]["status"] == "failed"


def test_subject_match_reported_when_recipient_differs(db_path):
    ledger.record_queued(db_path, "acct", ["a@x.com"], "Weekly", "Body1", tags=None, now=1000.0)
    block = ledger.check_block(db_path, ["c@z.com"], "Weekly", "Body2", now=1000.0 + 60)
    # recipient differs, but subject hash for the SAME recipient set must not collide;
    # subject key is recipient+subject, so different recipients => no match
    assert block is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ledger.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `ledger.py`**

```python
# src/email_mcp/ledger.py
import hashlib
import json

from email_mcp import store

WINDOW_SECONDS = 10 * 60
KEY_KINDS = ("recipient", "recipient_subject", "recipient_body")


def _norm_recipients(recipients):
    return ",".join(sorted(r.strip().lower() for r in recipients))


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def compute_keys(recipients, subject, body):
    rec = _norm_recipients(recipients)
    return {
        "recipient": _h(rec),
        "recipient_subject": _h(rec + "\x00" + (subject or "")),
        "recipient_body": _h(rec + "\x00" + (body or "")),
    }


def _now_iso(now):
    # store epoch seconds as text for simple window math
    return str(now)


def record_queued(db_path, account, recipients, subject, body, tags, now):
    keys = compute_keys(recipients, subject, body)
    rec = _norm_recipients(recipients)
    tags_json = json.dumps(tags) if tags is not None else None
    ids = []
    with store.connect(db_path) as conn:
        for kind in KEY_KINDS:
            cur = conn.execute(
                "INSERT INTO send_ledger(account, key_kind, key_hash, recipients, "
                "subject_excerpt, status, tags, sent_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (account, kind, keys[kind], rec, (subject or "")[:120],
                 "queued", tags_json, _now_iso(now)),
            )
            ids.append(cur.lastrowid)
    return ids


def _update_status(db_path, ids, status, now, message_id=None):
    with store.connect(db_path) as conn:
        for i in ids:
            conn.execute(
                "UPDATE send_ledger SET status=?, message_id=? WHERE id=?",
                (status, message_id, i),
            )


def mark_sent(db_path, ids, message_id, now):
    _update_status(db_path, ids, "sent", now, message_id)


def mark_failed(db_path, ids, now):
    _update_status(db_path, ids, "failed", now)


def check_block(db_path, recipients, subject, body, now):
    keys = compute_keys(recipients, subject, body)
    cutoff = now - WINDOW_SECONDS
    matched = []
    prior = None
    with store.connect(db_path) as conn:
        for kind in KEY_KINDS:
            row = conn.execute(
                "SELECT recipients, status, tags, sent_at, message_id "
                "FROM send_ledger WHERE key_kind=? AND key_hash=? "
                "AND CAST(sent_at AS REAL) >= ? ORDER BY id DESC LIMIT 1",
                (kind, keys[kind], cutoff),
            ).fetchone()
            if row:
                matched.append(kind)
                if prior is None:
                    prior = {
                        "recipients": row[0],
                        "status": row[1],
                        "tags": json.loads(row[2]) if row[2] else None,
                        "sent_at": row[3],
                        "message_id": row[4],
                    }
    if not matched:
        return None
    return {
        "status": "BLOCKED",
        "matched": matched,
        "reason": "duplicate send within 10 min window",
        "prior": prior,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ledger.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/ledger.py tests/test_ledger.py
git commit -m "feat: send-idempotency ledger (3 keys, status, 10-min window, failed-blocks)"
```

---

## Task 6: IMAP account — connect, list_folders

**Files:**
- Create: `src/email_mcp/providers/imap_account.py`
- Test: `tests/test_imap_account.py`

> IMAP is tested with a fake `imaplib`-shaped double injected via a `connect_fn` parameter, so no live server is needed. The double mimics the subset of `imaplib.IMAP4_SSL` we use.

- [ ] **Step 1: Write the failing test (folder listing)**

```python
# tests/test_imap_account.py
from email_mcp.providers import imap_account


class FakeIMAP:
    def __init__(self):
        self.selected = None
        self.readonly = None
        self.logged_in = False
        self.store_calls = []

    def login(self, user, pw):
        self.logged_in = True
        return ("OK", [b"ok"])

    def list(self, *a, **k):
        return ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren) "/" "Job"',
            b'(\\HasNoChildren) "/" "Job/Job Applications"',
            b'(\\HasNoChildren) "/" "Sent Messages"',
        ])

    def select(self, folder, readonly=False):
        self.selected = folder
        self.readonly = readonly
        return ("OK", [b"1"])

    def close(self):
        pass

    def logout(self):
        pass


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "imap_host": "h", "imap_port": 993}


def test_list_folders_parses_names():
    fake = FakeIMAP()
    folders = imap_account.list_folders(ACC, connect_fn=lambda acc: fake)
    assert "INBOX" in folders
    assert "Job/Job Applications" in folders
    assert "Sent Messages" in folders
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_imap_account.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write the connect + list_folders portion of `imap_account.py`**

```python
# src/email_mcp/providers/imap_account.py
import imaplib


def _default_connect(acc):
    imap = imaplib.IMAP4_SSL(acc["imap_host"], acc.get("imap_port", 993))
    imap.login(acc["email"], acc["password"])
    return imap


def _parse_folder_name(line):
    # line like: (\HasNoChildren) "/" "Job/Job Applications"
    s = line.decode() if isinstance(line, bytes) else line
    parts = s.split('"')
    if len(parts) >= 3:
        return parts[-2]
    return None


def list_folders(acc, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        status, data = imap.list()
        if status != "OK":
            return []
        names = []
        for line in data:
            name = _parse_folder_name(line)
            if name:
                names.append(name)
        return names
    finally:
        _safe_logout(imap)


def _safe_logout(imap):
    try:
        imap.logout()
    except Exception:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_imap_account.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/providers/imap_account.py tests/test_imap_account.py
git commit -m "feat: IMAP connect + folder listing"
```

---

## Task 7: IMAP fetch (PEEK + readonly), filter pass-through, cross-folder

**Files:**
- Modify: `src/email_mcp/providers/imap_account.py`
- Test: `tests/test_imap_account.py`

- [ ] **Step 1: Add the failing test for fetch never marking read**

```python
# tests/test_imap_account.py  (append)
import email as email_lib


def _raw_email(subject="Hi", frm="s@x.com", to="me@x.com", body="hello body"):
    m = email_lib.message.EmailMessage()
    m["Subject"] = subject
    m["From"] = frm
    m["To"] = to
    m["Message-ID"] = f"<{subject}@x>"
    m["Date"] = "Wed, 28 May 2026 10:00:00 +0000"
    m.set_content(body)
    return bytes(m)


class FakeIMAPWithMsgs(FakeIMAP):
    def __init__(self):
        super().__init__()
        self.fetch_specs = []

    def search(self, charset, *criteria):
        return ("OK", [b"1 2"])

    def fetch(self, num, spec):
        self.fetch_specs.append(spec)
        return ("OK", [(b"1 (BODY[])", _raw_email(subject=f"Msg{num.decode()}"))])


def test_fetch_uses_peek_and_readonly():
    fake = FakeIMAPWithMsgs()
    msgs = imap_account.fetch_folder(
        ACC, "INBOX", criteria=["SINCE", "01-Jan-2026"],
        connect_fn=lambda acc: fake)
    assert fake.readonly is True
    assert all("PEEK" in s for s in fake.fetch_specs)
    assert len(msgs) == 2
    assert msgs[0]["subject"].startswith("Msg")
    assert msgs[0]["body"] == "hello body\n"
    assert "message_id" in msgs[0] and "from_address" in msgs[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_imap_account.py::test_fetch_uses_peek_and_readonly -v`
Expected: FAIL — `fetch_folder` missing.

- [ ] **Step 3: Add `fetch_folder` to `imap_account.py`**

```python
# src/email_mcp/providers/imap_account.py  (add imports + function)
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from email_mcp import textutil


def _decode_hdr(value):
    out = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return " ".join(out)


def _parse_date(date_str):
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.now(timezone.utc)


def fetch_folder(acc, folder, criteria, connect_fn=_default_connect):
    """Fetch messages matching `criteria` from one folder. Never marks read."""
    imap = connect_fn(acc)
    out = []
    try:
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            return out
        status, data = imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return out
        for num in data[0].split():
            status, fetched = imap.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = email.message_from_bytes(fetched[0][1])
            out.append({
                "message_id": msg.get("Message-ID", f"{folder}-{num.decode()}"),
                "from_address": _decode_hdr(msg.get("From", "")),
                "to_address": _decode_hdr(msg.get("To", acc["email"])),
                "subject": _decode_hdr(msg.get("Subject", "")),
                "body": textutil.extract_text(msg),
                "received_date": _parse_date(msg.get("Date", "")).isoformat(),
                "folder": folder,
            })
        return out
    finally:
        try:
            imap.close()
        except Exception:
            pass
        _safe_logout(imap)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_imap_account.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/providers/imap_account.py tests/test_imap_account.py
git commit -m "feat: IMAP fetch_folder with PEEK+readonly (never marks read)"
```

---

## Task 8: IMAP mark (read/unread) + move

**Files:**
- Modify: `src/email_mcp/providers/imap_account.py`
- Test: `tests/test_imap_account.py`

- [ ] **Step 1: Add failing tests for mark + move**

```python
# tests/test_imap_account.py  (append)
class FakeIMAPMutating(FakeIMAPWithMsgs):
    def __init__(self):
        super().__init__()
        self.uid_calls = []

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "SEARCH":
            return ("OK", [b"7"])     # found UID 7
        return ("OK", [b"done"])


def test_mark_read_sets_seen_flag():
    fake = FakeIMAPMutating()
    res = imap_account.mark_message(
        ACC, "<Msg@x>", read=True, folders=["INBOX"], connect_fn=lambda acc: fake)
    assert res["read"] is True
    store_calls = [c for c in fake.uid_calls if c[0] == "STORE"]
    assert store_calls and "+FLAGS" in store_calls[0][1]
    assert "\\Seen" in store_calls[0][1][-1]


def test_move_uses_uid_move_or_copy():
    fake = FakeIMAPMutating()
    res = imap_account.move_message(
        ACC, "<Msg@x>", dest_folder="Archive", folders=["INBOX"],
        connect_fn=lambda acc: fake)
    assert res["dest_folder"] == "Archive"
    cmds = [c[0] for c in fake.uid_calls]
    assert "MOVE" in cmds or "COPY" in cmds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_imap_account.py -k "mark or move" -v`
Expected: FAIL — functions missing.

- [ ] **Step 3: Add `mark_message` + `move_message` (locate by Message-ID across folders)**

```python
# src/email_mcp/providers/imap_account.py  (append)

def _find_uid(imap, message_id):
    status, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    if status == "OK" and data and data[0]:
        return data[0].split()[0]
    return None


def _locate(imap, message_id, folders):
    for folder in folders:
        status, _ = imap.select(folder, readonly=False)
        if status != "OK":
            continue
        uid = _find_uid(imap, message_id)
        if uid:
            return folder, uid
    return None, None


def mark_message(acc, message_id, read, folders, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        folder, uid = _locate(imap, message_id, folders)
        if not uid:
            return {"error": "not_found", "message_id": message_id}
        op = "+FLAGS" if read else "-FLAGS"
        imap.uid("STORE", uid, op, "(\\Seen)")
        return {"message_id": message_id, "read": read, "folder": folder}
    finally:
        _safe_logout(imap)


def move_message(acc, message_id, dest_folder, folders, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        folder, uid = _locate(imap, message_id, folders)
        if not uid:
            return {"error": "not_found", "message_id": message_id}
        status, _ = imap.uid("MOVE", uid, dest_folder)
        if status != "OK":
            imap.uid("COPY", uid, dest_folder)
            imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            imap.expunge()
        return {"message_id": message_id, "dest_folder": dest_folder, "from_folder": folder}
    finally:
        _safe_logout(imap)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_imap_account.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/providers/imap_account.py tests/test_imap_account.py
git commit -m "feat: IMAP mark read/unread + move by Message-ID"
```

---

## Task 9: SMTP send (STARTTLS + requireTLS)

**Files:**
- Create: `src/email_mcp/providers/smtp_send.py`
- Test: `tests/test_smtp_send.py`

- [ ] **Step 1: Write the failing test (fake SMTP, assert STARTTLS used)**

```python
# tests/test_smtp_send.py
from email_mcp.providers import smtp_send


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = False
        self.sent = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, pw):
        self.logged_in = True

    def send_message(self, msg):
        self.sent = msg

    def quit(self):
        pass


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "smtp_host": "smtp.h", "smtp_port": 587}


def test_send_uses_starttls_and_login(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    mid = smtp_send.send(ACC, to=["x@y.com"], subject="Hi", body="Body")
    inst = FakeSMTP.instances[0]
    assert inst.started_tls is True
    assert inst.logged_in is True
    assert inst.sent["To"] == "x@y.com"
    assert inst.sent["From"] == "me@x.com"
    assert mid  # a Message-ID string is returned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_smtp_send.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `smtp_send.py`**

```python
# src/email_mcp/providers/smtp_send.py
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


def send(acc, to, subject, body):
    msg = EmailMessage()
    msg["From"] = acc["email"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    msg.set_content(body)

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    server = smtplib.SMTP(acc["smtp_host"], acc.get("smtp_port", 587), timeout=30)
    try:
        server.ehlo()
        server.starttls(context=context)   # requireTLS: we always STARTTLS
        server.ehlo()
        server.login(acc["email"], acc["password"])
        server.send_message(msg)
        return message_id
    finally:
        try:
            server.quit()
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_smtp_send.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/providers/smtp_send.py tests/test_smtp_send.py
git commit -m "feat: SMTP send with enforced STARTTLS"
```

---

## Task 10: get_emails — orchestration (cross-folder, cache, paginate, token cap)

**Files:**
- Modify: `src/email_mcp/server.py` (created here as a plain module first; FastMCP wiring in Task 13)
- Create: `src/email_mcp/email_ops.py` (pure logic, no MCP decorators — easy to test)
- Test: `tests/test_email_ops.py`

> Keep the testable orchestration in `email_ops.py`; `server.py` (Task 13) will be thin wrappers that call these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_ops.py
from email_mcp import email_ops


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "imap_host": "h", "imap_port": 993}


def _emails(n, body="word match here", subj="S"):
    return [{"message_id": f"<{i}>", "from_address": "s@x.com",
             "to_address": "me@x.com", "subject": f"{subj}{i}",
             "body": body, "received_date": "2026-05-28T10:00:00+00:00",
             "folder": "INBOX"} for i in range(n)]


def test_get_emails_paginates_and_caches(db_path):
    calls = {"n": 0}

    def fake_fetch(acc, folder, criteria, connect_fn=None):
        calls["n"] += 1
        return _emails(25) if folder == "INBOX" else []

    def fake_folders(acc, connect_fn=None):
        return ["INBOX", "Sent Messages"]

    r1 = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=fake_folders, now=1000.0)
    assert len(r1["emails"]) == 20
    assert r1["total_estimate"] == 25
    assert r1["from_cache"] is False

    r2 = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=2, page_size=20,
        fetch_fn=fake_fetch, folders_fn=fake_folders, now=1000.0 + 60)
    assert len(r2["emails"]) == 5
    assert r2["from_cache"] is True  # page 2 served from cache, no refetch


def test_query_filters_client_side(db_path):
    def fake_fetch(acc, folder, criteria, connect_fn=None):
        return [
            {"subject": "has needle", "body": "x", "from_address": "a", "to_address": "b",
             "message_id": "<1>", "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
            {"subject": "no match", "body": "y", "from_address": "a", "to_address": "b",
             "message_id": "<2>", "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
        ]
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query="needle", folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=lambda a, connect_fn=None: ["INBOX"], now=1000.0)
    assert out["total_estimate"] == 1
    assert out["emails"][0]["message_id"] == "<1>"


def test_excludes_sent_by_default(db_path):
    seen = []

    def fake_fetch(acc, folder, criteria, connect_fn=None):
        seen.append(folder)
        return []
    email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch,
        folders_fn=lambda a, connect_fn=None: ["INBOX", "Sent Messages", "Drafts"],
        now=1000.0)
    assert "Sent Messages" not in seen
    assert "Drafts" not in seen
    assert "INBOX" in seen
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_email_ops.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write `email_ops.py`**

```python
# src/email_mcp/email_ops.py
from datetime import datetime, timedelta, timezone

from email_mcp import cache, textutil
from email_mcp.providers import imap_account

DEFAULT_WINDOW_DAYS = 90
MAX_TOKENS = 100_000
SENT_DRAFT_NAMES = ("sent", "drafts", "sent messages", "sent items")


def _default_criteria(filters):
    """Pass filters through as IMAP criteria; default to SINCE 90 days."""
    if filters and "criteria" in filters:
        return list(filters["criteria"])
    since = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS))
    return ["SINCE", since.strftime("%d-%b-%Y")]


def _select_folders(all_folders, folders, include_sent):
    if folders:
        return folders
    out = []
    for f in all_folders:
        low = f.lower()
        is_sent = "sent" in low
        is_draft = "draft" in low
        if is_draft:
            continue
        if is_sent and not include_sent:
            continue
        out.append(f)
    return out


def _matches_query(msg, query):
    if not query:
        return True
    q = query.lower()
    return q in (msg.get("subject", "").lower()) or q in (msg.get("body", "").lower())


def get_emails(db_path, acc, filters, query, folders, include_sent,
               strip_to_text, page, page_size,
               fetch_fn=imap_account.fetch_folder,
               folders_fn=imap_account.list_folders, now=None):
    key = cache.make_key(
        acc["name"],
        {"filters": filters, "query": query, "folders": folders},
        include_sent, strip_to_text,
    )
    cached = cache.get(db_path, key, now=now)
    from_cache = cached is not None
    if cached is None:
        all_folders = folders_fn(acc)
        target = _select_folders(all_folders, folders, include_sent)
        criteria = _default_criteria(filters)
        merged = []
        for folder in target:
            for msg in fetch_fn(acc, folder, criteria):
                if not _matches_query(msg, query):
                    continue
                if strip_to_text:
                    msg = {**msg, "body": textutil.strip_non_ascii(msg.get("body", ""))}
                merged.append(msg)
        merged.sort(key=lambda m: m.get("received_date", ""), reverse=True)
        cached = merged
        cache.set(db_path, key, cached, now=now)

    total = len(cached)
    start = (page - 1) * page_size
    page_items = cached[start:start + page_size]
    page_items, truncated = textutil.truncate_to_budget(page_items, MAX_TOKENS)
    return {
        "emails": page_items,
        "page": page,
        "page_size": page_size,
        "total_estimate": total,
        "truncated": truncated,
        "from_cache": from_cache,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_email_ops.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/email_ops.py tests/test_email_ops.py
git commit -m "feat: get_emails orchestration (cross-folder, cache, paginate, token cap)"
```

---

## Task 11: send_email orchestration (ledger-guarded)

**Files:**
- Modify: `src/email_mcp/email_ops.py`
- Test: `tests/test_email_ops.py`

- [ ] **Step 1: Add failing tests**

```python
# tests/test_email_ops.py  (append)
from email_mcp import ledger


def test_send_blocks_duplicate(db_path):
    sent = []

    def fake_send(acc, to, subject, body):
        sent.append((to, subject, body))
        return "<mid-1@x>"

    r1 = email_ops.send_email(
        db_path, ACC, to=["x@y.com"], subject="S", body="B",
        tags={"c": 1}, send_fn=fake_send, now=1000.0)
    assert r1["status"] == "sent"
    assert r1["message_id"] == "<mid-1@x>"

    r2 = email_ops.send_email(
        db_path, ACC, to=["x@y.com"], subject="totally different", body="other",
        tags=None, send_fn=fake_send, now=1000.0 + 30)
    assert r2["status"] == "BLOCKED"
    assert "recipient" in r2["matched"]
    assert r2["prior"]["tags"] == {"c": 1}
    assert len(sent) == 1  # second send never hit SMTP


def test_send_failure_records_failed_and_blocks(db_path):
    def boom(acc, to, subject, body):
        raise RuntimeError("smtp down")

    r1 = email_ops.send_email(
        db_path, ACC, to=["z@y.com"], subject="S", body="B",
        tags=None, send_fn=boom, now=2000.0)
    assert r1["status"] == "failed"

    r2 = email_ops.send_email(
        db_path, ACC, to=["z@y.com"], subject="S", body="B",
        tags=None, send_fn=lambda *a: "<x>", now=2000.0 + 30)
    assert r2["status"] == "BLOCKED"
    assert r2["prior"]["status"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_email_ops.py -k send -v`
Expected: FAIL — `send_email` missing.

- [ ] **Step 3: Add `send_email` to `email_ops.py`**

```python
# src/email_mcp/email_ops.py  (add import + function)
import time

from email_mcp import ledger
from email_mcp.providers import smtp_send


def send_email(db_path, acc, to, subject, body, tags,
               send_fn=smtp_send.send, now=None):
    now = time.time() if now is None else now
    block = ledger.check_block(db_path, to, subject, body, now=now)
    if block is not None:
        return block
    ids = ledger.record_queued(db_path, acc["name"], to, subject, body, tags, now=now)
    try:
        message_id = send_fn(acc, to, subject, body)
    except Exception as e:
        ledger.mark_failed(db_path, ids, now=now)
        return {"status": "failed", "error": str(e)}
    ledger.mark_sent(db_path, ids, message_id, now=now)
    return {"status": "sent", "message_id": message_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_email_ops.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/email_mcp/email_ops.py tests/test_email_ops.py
git commit -m "feat: send_email orchestration guarded by idempotency ledger"
```

---

## Task 12: FastMCP server — register the 8 tools

**Files:**
- Create: `src/email_mcp/server.py`, `src/email_mcp/runtime.py` (resolves paths/account)
- Test: `tests/test_server_tools.py`

> `server.py` tools must stay thin. `runtime.py` resolves the DB path, accounts file, and the
> effective account so tools are one-liners. Tests call the underlying functions in
> `email_ops`/`imap_account` (already covered); here we test `runtime` resolution and that
> `server` imports/register without error.

- [ ] **Step 1: Write the failing test for runtime resolution**

```python
# tests/test_server_tools.py
import os
from email_mcp import runtime


def test_paths_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("EMAIL_MCP_ACCOUNTS", str(tmp_path / "accounts.yml"))
    assert runtime.db_path().endswith("s.db")
    assert runtime.accounts_file().endswith("accounts.yml")


def test_server_module_imports():
    import email_mcp.server as srv
    # FastMCP app object exists and tools are registered
    assert hasattr(srv, "mcp")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_server_tools.py -v`
Expected: FAIL — modules missing.

- [ ] **Step 3: Write `runtime.py`**

```python
# src/email_mcp/runtime.py
import os
from pathlib import Path

from email_mcp import accounts, store

_DEFAULT_DB = Path.home() / ".local/state/email-mcp/state.db"
_DEFAULT_ACCOUNTS = Path(__file__).resolve().parents[2] / "config/accounts.yml"


def db_path():
    p = os.environ.get("EMAIL_MCP_DB", str(_DEFAULT_DB))
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    store.init_db(p)
    return p


def accounts_file():
    return os.environ.get("EMAIL_MCP_ACCOUNTS", str(_DEFAULT_ACCOUNTS))


def effective_account(name=None):
    af, db = accounts_file(), db_path()
    if name is None:
        name = accounts.resolve_default(af, db)
    return accounts.get_account(af, name)
```

- [ ] **Step 4: Write `server.py`**

```python
# src/email_mcp/server.py
from mcp.server.fastmcp import FastMCP

from email_mcp import accounts, email_ops, runtime
from email_mcp.providers import imap_account

mcp = FastMCP("email-mcp")


@mcp.tool()
def list_accounts() -> list:
    """List configured email accounts and which one is the default."""
    af, db = runtime.accounts_file(), runtime.db_path()
    default = accounts.resolve_default(af, db)
    return [{"name": a["name"], "email": a["email"], "default": a["name"] == default}
            for a in accounts.load_accounts(af)]


@mcp.tool()
def set_default_account(name: str) -> dict:
    """Set the default account used when a tool omits `account`."""
    accounts.set_default(runtime.accounts_file(), runtime.db_path(), name)
    return {"default": name}


@mcp.tool()
def list_folders(account: str = None) -> list:
    """List mail folders for an account (default account if omitted)."""
    acc = runtime.effective_account(account)
    return imap_account.list_folders(acc)


@mcp.tool()
def get_emails(account: str = None, filters: dict = None, query: str = None,
               folders: list = None, include_sent: bool = False,
               strip_to_text: bool = False, page: int = 1,
               page_size: int = 20) -> dict:
    """Fetch/search emails. Never marks mail read. Search is `query` (client-side,
    across folders). `filters.criteria` is passed through as IMAP search keys.
    Results cached 60 min; paginate with page/page_size (<= ~100k tokens/page)."""
    acc = runtime.effective_account(account)
    return email_ops.get_emails(
        runtime.db_path(), acc, filters=filters, query=query, folders=folders,
        include_sent=include_sent, strip_to_text=strip_to_text,
        page=page, page_size=page_size)


@mcp.tool()
def send_email(to: list, subject: str, body: str, account: str = None,
               tags: dict = None) -> dict:
    """Send an email. Idempotent: a duplicate (same recipients, or same
    recipients+subject, or same recipients+body) within 10 minutes is BLOCKED and
    returns the prior send's status/tags instead of resending."""
    acc = runtime.effective_account(account)
    return email_ops.send_email(
        runtime.db_path(), acc, to=to, subject=subject, body=body, tags=tags)


@mcp.tool()
def mark_email(message_id: str, read: bool, account: str = None,
               folders: list = None) -> dict:
    """Mark a message read (read=true) or unread (read=false) by Message-ID."""
    acc = runtime.effective_account(account)
    search_folders = folders or imap_account.list_folders(acc)
    return imap_account.mark_message(acc, message_id, read=read, folders=search_folders)


@mcp.tool()
def move_email(message_id: str, dest_folder: str, account: str = None,
               folders: list = None) -> dict:
    """Move a message (by Message-ID) to dest_folder within the same account."""
    acc = runtime.effective_account(account)
    search_folders = folders or imap_account.list_folders(acc)
    return imap_account.move_message(acc, message_id, dest_folder=dest_folder,
                                     folders=search_folders)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_server_tools.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add src/email_mcp/server.py src/email_mcp/runtime.py tests/test_server_tools.py
git commit -m "feat: FastMCP server exposing all 8 email tools"
```

---

## Task 13: README + MCP registration + manual live smoke

**Files:**
- Create: `README.md`, `tests/live_smoke_test.py`

- [ ] **Step 1: Write `README.md`**

````markdown
# email-mcp

Local MCP server for multi-account IMAP/SMTP email (iCloud + Gmail via app-specific
passwords). Never marks mail read. Cross-folder search, 60-min cache, idempotent sends.

## Setup
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e .
# edit config/accounts.yml with your accounts
.venv/bin/python -m email_mcp.setup_cli icloud-personal   # stores app password in Keychain
```

## Register with Claude Code
```bash
claude mcp add email --scope user -- /path/to/email-mcp/.venv/bin/python -m email_mcp.server
```

## Tools
list_accounts, set_default_account, list_folders, get_emails (search folded in),
send_email (idempotent), mark_email, move_email.

## Get an app-specific password
appleid.apple.com -> Sign-In & Security -> App-Specific Passwords (iCloud).
Gmail: myaccount.google.com -> Security -> App passwords.
````

- [ ] **Step 2: Write the env-gated live smoke test**

```python
# tests/live_smoke_test.py
"""Manual live test. NOT run in CI. Requires a real account in accounts.yml and
its password in Keychain. Run:  EMAIL_MCP_LIVE=1 .venv/bin/python tests/live_smoke_test.py
Reads only (never sends) so it is safe to run repeatedly."""
import os
import sys

from email_mcp import email_ops, runtime


def main():
    if os.environ.get("EMAIL_MCP_LIVE") != "1":
        print("Set EMAIL_MCP_LIVE=1 to run.")
        sys.exit(0)
    acc = runtime.effective_account()
    print("Account:", acc["name"], acc["email"])
    res = email_ops.get_emails(
        runtime.db_path(), acc, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=True, page=1, page_size=3)
    print("total_estimate:", res["total_estimate"], "from_cache:", res["from_cache"])
    for e in res["emails"]:
        print("-", e["subject"][:60], "|", e["from_address"][:40])


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the smoke test manually (after real setup)**

Run: `EMAIL_MCP_LIVE=1 .venv/bin/python tests/live_smoke_test.py`
Expected: prints the account and up to 3 INBOX subjects; **verify in the iCloud web UI that those emails remain unread.**

- [ ] **Step 4: Commit**

```bash
git add README.md tests/live_smoke_test.py
git commit -m "docs: README + manual live smoke test"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Tool 1 list_accounts → Task 12 ✓ | Tool 2 set_default → Task 12 ✓ (persisted via Task 1)
- Tool 3 get_emails (filters pass-through, 60-min cache, pagination, 20 default, 100k cap,
  strip non-ASCII, never-mark-read, include_sent) → Tasks 7+10 ✓
- Tool 4 search folded into get_emails (`query`, cross-folder) → Task 10 ✓
- Tool 5 send_email idempotent (3 keys, recipient hard-block, failed-still-blocks, tags,
  status, returns prior record) → Tasks 5+11 ✓
- Tool 6 mark read/unread → Task 8 ✓ | Tool 7 move → Task 8 ✓ | Tool 8 list_folders → Task 6 ✓
- Keychain creds → Task 2 ✓ | connect-per-call → Tasks 6–9 ✓ | TLS enforced → Task 9 ✓
- SQLite WAL shared state → Task 1 ✓ | 90-day search window → Task 10 ✓
- Notes/Calendar/Reminders: out of scope (spec §1) — no task, intentional.

**Placeholder scan:** no TBD/TODO; every code step has complete code. ✓

**Type/name consistency:** `fetch_folder`, `list_folders`, `mark_message`, `move_message`,
`get_emails`, `send_email`, `check_block`, `record_queued`, `mark_sent`, `mark_failed`,
`compute_keys`, `make_key`, `cache.get/set`, `runtime.effective_account` — names match across
tasks. ✓

**Known follow-ups (post-v1, documented, not gaps):** retry/cancel of failed sends;
connection pooling if throughput demands it; richer IMAP filter translation; attachments;
reply/threading headers; Calendar/Reminders via CalDAV (separate spec).
