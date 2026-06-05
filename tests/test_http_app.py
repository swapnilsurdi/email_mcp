import time

import pytest
from fastapi.testclient import TestClient

from email_mcp.http import app as http_app
from email_mcp.http import db

MK = "test-master-key"


@pytest.fixture
def client(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    application = http_app.create_app(db_path_fn=lambda: p,
                                      master_key_fn=lambda: MK,
                                      base_url="https://email-mcp.test")
    with TestClient(application) as c:
        c.db_path = p
        yield c


def login(client, matrix_user="@admin:chat"):
    uid = db.get_or_create_user(client.db_path, matrix_user, time.time())
    return db.issue_login_token(client.db_path, uid, time.time())


def auth_hdr(token):
    return {"Authorization": f"Bearer {token}"}


MB = {"mailbox": {"email": "bob@icloud.com", "password": "app-pw",
                  "imap_host": "imap.x", "imap_port": 993,
                  "smtp_host": "smtp.x", "smtp_port": 587},
      "policy": {"allowed_recipients": ["ok@x\\.com"]}}


# ---- public ---------------------------------------------------------------------------

def test_info_and_health_public(client):
    r = client.get("/info")
    assert r.status_code == 200
    body = r.json()
    assert body["mcp"]["endpoint"].endswith("/mcp")
    assert "read" in body["scopes"]
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


# ---- setup ----------------------------------------------------------------------------

def test_setup_requires_login(client):
    assert client.post("/api/setup", json=MB).status_code == 401


def test_setup_create_update_and_email_uniqueness(client):
    t = login(client)
    r = client.post("/api/setup", json=MB, headers=auth_hdr(t))
    assert r.status_code == 200 and r.json()["created"] is True
    mid = r.json()["mailbox_id"]

    # same user, same email -> update in place
    upd = {"mailbox": {"email": "bob@icloud.com", "imap_host": "imap2.x"}}
    r = client.post("/api/setup", json=upd, headers=auth_hdr(t))
    assert r.json() == {"mailbox_id": mid, "email": "bob@icloud.com", "updated": True}

    # different user, same email -> blocked
    t2 = login(client, "@mallory:chat")
    r = client.post("/api/setup", json=MB, headers=auth_hdr(t2))
    assert r.status_code == 409

    # delete frees the email for someone else
    r = client.delete(f"/api/mailboxes/{mid}", headers=auth_hdr(t))
    assert r.json()["deleted"] is True
    r = client.post("/api/setup", json=MB, headers=auth_hdr(t2))
    assert r.status_code == 200 and r.json()["created"] is True


def test_mailboxes_listing_and_disconnect(client):
    t = login(client)
    client.post("/api/setup", json=MB, headers=auth_hdr(t))
    r = client.get("/api/mailboxes", headers=auth_hdr(t))
    [m] = r.json()
    assert m["email"] == "bob@icloud.com" and m["connected"] is True
    assert m["policy"] == {"allowed_recipients": ["ok@x\\.com"]}
    r = client.post(f"/api/mailboxes/{m['id']}/disconnect", headers=auth_hdr(t))
    assert r.json()["disconnected"] is True
    [m] = client.get("/api/mailboxes", headers=auth_hdr(t)).json()
    assert m["connected"] is False


# ---- agent keys -------------------------------------------------------------------------

def test_token_lifecycle_via_login(client):
    t = login(client)
    client.post("/api/setup", json=MB, headers=auth_hdr(t))
    r = client.post("/api/tokens", json={"label": "orch", "scopes": "read,send,mint"},
                    headers=auth_hdr(t))
    assert r.status_code == 200
    key = r.json()["token"]
    tid = r.json()["token_id"]

    rows = client.get("/api/tokens", headers=auth_hdr(t)).json()
    assert rows[0]["label"] == "orch" and rows[0]["prefix"] == key[:6]

    r = client.patch(f"/api/tokens/{tid}", json={"active": False}, headers=auth_hdr(t))
    assert r.json()["active"] == 0
    r = client.patch(f"/api/tokens/{tid}", json={"active": True}, headers=auth_hdr(t))
    assert r.json()["active"] == 1

    r = client.delete(f"/api/tokens/{tid}", headers=auth_hdr(t))
    assert r.json()["revoked"] is True
    # a revoked key cannot be resumed
    r = client.patch(f"/api/tokens/{tid}", json={"active": True}, headers=auth_hdr(t))
    assert r.json()["active"] == 0 and r.json()["revoked"] == 1


def test_mint_key_can_mint_scoped_subkeys(client):
    t = login(client)
    client.post("/api/setup", json=MB, headers=auth_hdr(t))
    orch = client.post("/api/tokens", json={"label": "orch", "scopes": "read,send,mint"},
                       headers=auth_hdr(t)).json()["token"]

    # orchestrator key mints a read-only subagent key
    r = client.post("/api/tokens", json={"label": "sub", "scopes": ["read"]},
                    headers=auth_hdr(orch))
    assert r.status_code == 200 and r.json()["scopes"] == ["read"]

    # cannot grant mint/admin, nor scopes it doesn't hold (write)
    assert client.post("/api/tokens", json={"label": "x", "scopes": ["mint"]},
                       headers=auth_hdr(orch)).status_code == 403
    assert client.post("/api/tokens", json={"label": "x", "scopes": ["write"]},
                       headers=auth_hdr(orch)).status_code == 403

    # a read-only key cannot mint at all
    ro = client.post("/api/tokens", json={"label": "ro", "scopes": ["read"]},
                     headers=auth_hdr(t)).json()["token"]
    assert client.post("/api/tokens", json={"label": "x", "scopes": ["read"]},
                       headers=auth_hdr(ro)).status_code == 401


# ---- dashboard --------------------------------------------------------------------------

def test_dashboard_login_flow(client):
    r = client.get("/")
    assert "How to sign in" in r.text

    t = login(client)
    r = client.get(f"/?token={t}", follow_redirects=False)
    assert r.status_code == 303 and http_app.SESSION_COOKIE in r.cookies

    client.cookies.set(http_app.SESSION_COOKIE, t)
    r = client.get("/")
    assert "Connect a mailbox" in r.text

    r = client.post("/dash/mailbox", data={
        "email": "bob@icloud.com", "password": "pw", "imap_host": "i",
        "imap_port": "993", "smtp_host": "s", "smtp_port": "587",
        "allowed_recipients": "ok@x\\.com"})
    assert "Mailbox saved." in r.text and "bob@icloud.com" in r.text

    r = client.post("/dash/tokens", data={"label": "orch",
                                          "scopes": ["read", "send"]})
    assert "shown once" in r.text


def test_dashboard_invalid_token_shows_error(client):
    r = client.get("/?token=bogus")
    assert "invalid or expired" in r.text


# ---- MCP over HTTP ------------------------------------------------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


def test_mcp_endpoint_requires_key(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers=MCP_HEADERS, follow_redirects=False)
    assert r.status_code == 401          # and no 307 round-trip for the bare /mcp path


def test_mcp_endpoint_with_key_lists_tools(client):
    t = login(client)
    client.post("/api/setup", json=MB, headers=auth_hdr(t))
    key = client.post("/api/tokens", json={"label": "a", "scopes": "read"},
                      headers=auth_hdr(t)).json()["token"]
    r = client.post("/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={**MCP_HEADERS, **auth_hdr(key)})
    assert r.status_code == 200
    assert "get_emails" in r.text and "send_email" in r.text


def test_mcp_tools_call_binds_principal_end_to_end(client):
    """The full path: HTTP POST -> auth middleware -> ContextVar -> tool -> response."""
    t = login(client)
    client.post("/api/setup", json=MB, headers=auth_hdr(t))
    key = client.post("/api/tokens", json={"label": "a", "scopes": "read"},
                      headers=auth_hdr(t)).json()["token"]
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_accounts", "arguments": {}}}
    r = client.post("/mcp", json=call, headers={**MCP_HEADERS, **auth_hdr(key)})
    assert r.status_code == 200
    assert "bob@icloud.com" in r.text          # the key's bound mailbox

    # a scope the key lacks comes back as a structured error, not an exception
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "send_email",
                       "arguments": {"to": ["ok@x.com"], "subject": "s", "body": "b"}}}
    r = client.post("/mcp", json=call, headers={**MCP_HEADERS, **auth_hdr(key)})
    assert r.status_code == 200 and "forbidden" in r.text
