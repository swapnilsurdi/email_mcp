"""Multi-tenant state for the HTTP service, layered on the same SQLite DB/WAL pattern as
`email_mcp.store` (reuses `store.connect`; tables are additive and created idempotently).

Time fields are stored as epoch seconds (float) and a `now` is injectable everywhere, so
expiry/deadline logic is unit-testable without sleeping. Tokens (login + agent keys) are
stored only as SHA-256 hashes; the raw value is returned exactly once at creation.
"""
import hashlib
import json
import secrets

from email_mcp import store
from email_mcp.http import crypto

HTTP_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  matrix_user TEXT UNIQUE NOT NULL,
  created_at  REAL NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS mailboxes (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  name         TEXT,
  email        TEXT UNIQUE NOT NULL,
  imap_host    TEXT, imap_port INTEGER,
  smtp_host    TEXT, smtp_port INTEGER,
  enc_password BLOB, salt BLOB,
  policy_json  TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  consumed   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  mailbox_id   INTEGER NOT NULL,
  label        TEXT,
  scopes       TEXT NOT NULL DEFAULT '',
  token_hash   TEXT UNIQUE NOT NULL,
  prefix       TEXT,
  active       INTEGER NOT NULL DEFAULT 1,
  revoked      INTEGER NOT NULL DEFAULT 0,
  created_by   TEXT,
  created_at   REAL NOT NULL,
  last_used_at REAL,
  cnt_read     INTEGER NOT NULL DEFAULT 0,
  cnt_search   INTEGER NOT NULL DEFAULT 0,
  cnt_send     INTEGER NOT NULL DEFAULT 0,
  cnt_blocked  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approvals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  auth_token_id   INTEGER,
  mailbox_id      INTEGER NOT NULL,
  recipient       TEXT,
  subject         TEXT,
  body_excerpt    TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',
  matrix_event_id TEXT,
  payload_json    TEXT,
  requested_at    REAL NOT NULL,
  decided_at      REAL
);
CREATE TABLE IF NOT EXISTS service_identity (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""

# Counter columns an agent key accrues (whitelist guards the dynamic UPDATE below).
USAGE_FIELDS = ("read", "search", "send", "blocked")
# read: fetch/search/download · write: mark/move · send: send_email ·
# mint: manage agent keys ("orchestrator") · admin: implies all + mailbox config
VALID_SCOPES = ("read", "write", "send", "mint", "admin")


class EmailTaken(ValueError):
    """A still-active mailbox already owns this email address."""


def init_http_tables(db_path):
    store.init_db(db_path)              # ensure the base tables exist too
    with store.connect(db_path) as conn:
        conn.executescript(HTTP_SCHEMA)


# ---- token helpers (hash-at-rest) -------------------------------------------------

def new_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_dict(cur, row):
    if row is None:
        return None
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


# ---- users ------------------------------------------------------------------------

def get_or_create_user(db_path, matrix_user, now):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT id FROM users WHERE matrix_user=?", (matrix_user,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            "INSERT INTO users(matrix_user, created_at) VALUES(?, ?)",
            (matrix_user, now))
        return cur.lastrowid


def get_user(db_path, user_id):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        return _row_to_dict(cur, cur.fetchone())


# ---- mailboxes --------------------------------------------------------------------

def email_in_use(db_path, email):
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT 1 FROM mailboxes WHERE email=? AND status='active'", (email,))
        return cur.fetchone() is not None


def create_mailbox(db_path, user_id, name, email, imap_host, imap_port,
                   smtp_host, smtp_port, password, policy=None, now=0.0,
                   master_key=None):
    """Create an active mailbox, encrypting the password. Raises EmailTaken if an active
    mailbox already owns `email` (one unique email across the system)."""
    if email_in_use(db_path, email):
        raise EmailTaken(email)
    salt, enc = crypto.encrypt_secret(password, master_key=master_key)
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO mailboxes(user_id, name, email, imap_host, imap_port, "
            "smtp_host, smtp_port, enc_password, salt, policy_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, name or email, email, imap_host, imap_port, smtp_host, smtp_port,
             enc, salt, json.dumps(policy or {}), now))
        return cur.lastrowid


def set_mailbox_password(db_path, mailbox_id, password, master_key=None):
    salt, enc = crypto.encrypt_secret(password, master_key=master_key)
    with store.connect(db_path) as conn:
        conn.execute("UPDATE mailboxes SET enc_password=?, salt=? WHERE id=?",
                     (enc, salt, mailbox_id))


def set_mailbox_policy(db_path, mailbox_id, policy):
    with store.connect(db_path) as conn:
        conn.execute("UPDATE mailboxes SET policy_json=? WHERE id=?",
                     (json.dumps(policy or {}), mailbox_id))


def get_mailbox(db_path, mailbox_id):
    """Mailbox row as a dict (password fields included as raw bytes, not decrypted)."""
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,))
        return _row_to_dict(cur, cur.fetchone())


def list_mailboxes_for_user(db_path, user_id):
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM mailboxes WHERE user_id=? AND status='active' "
            "ORDER BY id", (user_id,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def mailbox_account(db_path, mailbox_id, master_key=None):
    """The `account` dict the core ops expect, with the password DECRYPTED. Returns None
    if the mailbox is missing or its password has been erased (logged out)."""
    mb = get_mailbox(db_path, mailbox_id)
    if mb is None or mb["status"] != "active" or not mb["enc_password"]:
        return None
    password = crypto.decrypt_secret(mb["salt"], mb["enc_password"], master_key=master_key)
    return {
        "name": f"mbx-{mailbox_id}",
        "email": mb["email"],
        "imap_host": mb["imap_host"], "imap_port": mb["imap_port"],
        "smtp_host": mb["smtp_host"], "smtp_port": mb["smtp_port"],
        "password": password,
    }


def mailbox_policy_dict(db_path, mailbox_id):
    mb = get_mailbox(db_path, mailbox_id)
    if mb is None:
        return {}
    return json.loads(mb["policy_json"] or "{}")


def erase_mailbox_password(db_path, mailbox_id):
    """Logout: drop the stored ciphertext so the password is no longer recoverable."""
    with store.connect(db_path) as conn:
        conn.execute("UPDATE mailboxes SET enc_password=NULL, salt=NULL WHERE id=?",
                     (mailbox_id,))


def delete_mailbox(db_path, mailbox_id, now, suffix=None):
    """Soft-delete: erase the password and TOMBSTONE the email (append a random suffix)
    so the address is freed for the same or another user to register again."""
    suffix = suffix or secrets.token_hex(3)
    with store.connect(db_path) as conn:
        conn.execute(
            "UPDATE mailboxes SET status='deleted', enc_password=NULL, salt=NULL, "
            "email = email || ? WHERE id=?",
            (f"#deleted-{suffix}", mailbox_id))


# ---- login tokens (24h dashboard bootstrap) ---------------------------------------

def issue_login_token(db_path, user_id, now, ttl=86400):
    raw = new_token()
    with store.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO login_tokens(token_hash, user_id, created_at, expires_at) "
            "VALUES(?,?,?,?)", (hash_token(raw), user_id, now, now + ttl))
    return raw


def validate_login_token(db_path, raw, now):
    """Return the user_id for a non-expired, unredeemed login token, else None. Used for
    Bearer-header API auth; once the token has been redeemed for a dashboard session
    (consume_login_token) it no longer validates anywhere."""
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT user_id, expires_at FROM login_tokens "
            "WHERE token_hash=? AND consumed=0", (hash_token(raw),))
        row = cur.fetchone()
        if not row or row[1] < now:
            return None
        return row[0]


def consume_login_token(db_path, raw, now):
    """Single-use redemption of a login token (the dashboard sign-in link): atomically
    mark it consumed and return its user_id, or None if unknown/expired/already used."""
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE login_tokens SET consumed=1 "
            "WHERE token_hash=? AND consumed=0 AND expires_at>=?",
            (hash_token(raw), now))
        if cur.rowcount != 1:
            return None
        cur = conn.execute("SELECT user_id FROM login_tokens WHERE token_hash=?",
                           (hash_token(raw),))
        return cur.fetchone()[0]


# ---- dashboard sessions (minted on login-token redemption; never appear in URLs) ----

def create_session(db_path, user_id, now, ttl=86400):
    raw = new_token()
    with store.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
            "VALUES(?,?,?,?)", (hash_token(raw), user_id, now, now + ttl))
    return raw


def validate_session(db_path, raw, now):
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash=?",
            (hash_token(raw),))
        row = cur.fetchone()
        if not row or row[1] < now:
            return None
        return row[0]


def delete_session(db_path, raw):
    """Sign-out: drop the session row so the cookie value is dead server-side."""
    with store.connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(raw),))


def delete_sessions_for_user(db_path, user_id):
    """The bot's `logout`: end every dashboard session the user has anywhere."""
    with store.connect(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def consume_login_tokens_for_user(db_path, user_id):
    """The bot's `logout`: void any sign-in links the user hasn't redeemed yet."""
    with store.connect(db_path) as conn:
        conn.execute("UPDATE login_tokens SET consumed=1 WHERE user_id=? AND consumed=0",
                     (user_id,))


# ---- agent auth tokens ------------------------------------------------------------

def _norm_scopes(scopes):
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    out = [s.strip() for s in scopes if s and s.strip()]
    bad = [s for s in out if s not in VALID_SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s): {bad}; valid: {VALID_SCOPES}")
    return ",".join(dict.fromkeys(out))           # de-dupe, preserve order


def create_auth_token(db_path, mailbox_id, label, scopes, created_by, now):
    raw = new_token()
    scopes_csv = _norm_scopes(scopes)
    prefix = raw[:6]
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO auth_tokens(mailbox_id, label, scopes, token_hash, prefix, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (mailbox_id, label, scopes_csv, hash_token(raw), prefix, created_by, now))
        return raw, cur.lastrowid


def principal_for_raw(db_path, raw):
    """Resolve a raw agent key to a principal dict, or None if unknown. `scopes` is a set.
    `active` reflects the paused/deactivated flag (caller decides 403 vs allow)."""
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, mailbox_id, scopes, active FROM auth_tokens WHERE token_hash=?",
            (hash_token(raw),))
        row = cur.fetchone()
        if not row:
            return None
        return {"token_id": row[0], "mailbox_id": row[1],
                "scopes": set(s for s in (row[2] or "").split(",") if s),
                "active": bool(row[3])}


def get_auth_token(db_path, token_id):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM auth_tokens WHERE id=?", (token_id,))
        return _row_to_dict(cur, cur.fetchone())


def list_auth_tokens(db_path, mailbox_id):
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM auth_tokens WHERE mailbox_id=? ORDER BY id", (mailbox_id,))
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def set_auth_token_active(db_path, token_id, active):
    """Pause/resume a key. A revoked key can never be re-activated."""
    with store.connect(db_path) as conn:
        conn.execute("UPDATE auth_tokens SET active=? WHERE id=? AND revoked=0",
                     (1 if active else 0, token_id))


def revoke_auth_token(db_path, token_id):
    """Permanently deactivate a key (irreversible — pause via set_auth_token_active)."""
    with store.connect(db_path) as conn:
        conn.execute("UPDATE auth_tokens SET active=0, revoked=1 WHERE id=?",
                     (token_id,))


def bump_usage(db_path, token_id, field, now, n=1):
    if field not in USAGE_FIELDS:
        raise ValueError(f"unknown usage field: {field}")
    with store.connect(db_path) as conn:
        conn.execute(
            f"UPDATE auth_tokens SET cnt_{field}=cnt_{field}+?, last_used_at=? "
            f"WHERE id=?", (n, now, token_id))


# ---- approvals --------------------------------------------------------------------

def create_approval(db_path, auth_token_id, mailbox_id, recipient, subject,
                    body_excerpt, payload, now):
    with store.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO approvals(auth_token_id, mailbox_id, recipient, subject, "
            "body_excerpt, payload_json, requested_at) VALUES(?,?,?,?,?,?,?)",
            (auth_token_id, mailbox_id, recipient, subject, body_excerpt,
             json.dumps(payload or {}), now))
        return cur.lastrowid


def set_approval_event(db_path, approval_id, event_id):
    with store.connect(db_path) as conn:
        conn.execute("UPDATE approvals SET matrix_event_id=? WHERE id=?",
                     (event_id, approval_id))


def resolve_approval(db_path, approval_id, status, now):
    with store.connect(db_path) as conn:
        conn.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                     (status, now, approval_id))


def get_approval(db_path, approval_id):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return _row_to_dict(cur, cur.fetchone())


def get_approval_by_event(db_path, event_id):
    """The approval whose Matrix preview message a reaction points at."""
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM approvals WHERE matrix_event_id=?",
                           (event_id,))
        return _row_to_dict(cur, cur.fetchone())


def update_approval_payload(db_path, approval_id, payload):
    """Persist the send outcome alongside the original request (payload_json holds
    {'request': ..., 'result': ...})."""
    with store.connect(db_path) as conn:
        conn.execute("UPDATE approvals SET payload_json=? WHERE id=?",
                     (json.dumps(payload or {}), approval_id))


def list_pending_approvals(db_path):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id")
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


# ---- service identity (the bot's own persisted Matrix creds) ----------------------

def set_service_identity(db_path, key, value):
    with store.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_identity(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_service_identity(db_path, key):
    with store.connect(db_path) as conn:
        cur = conn.execute("SELECT value FROM service_identity WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
