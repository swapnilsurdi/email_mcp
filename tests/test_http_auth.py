import asyncio

import pytest

from email_mcp.http import auth, db

MK = "test-master-key"


@pytest.fixture
def hdb(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    return p


@pytest.fixture
def key_and_id(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@x.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    raw, tid = db.create_auth_token(hdb, mid, "agent", "read,send", "@bob:chat", now=1.0)
    return raw, tid


def test_parse_bearer():
    assert auth.parse_bearer("Bearer abc") == "abc"
    assert auth.parse_bearer("bearer abc") == "abc"
    assert auth.parse_bearer("Basic abc") is None
    assert auth.parse_bearer("") is None
    assert auth.parse_bearer("Bearer ") is None


def test_resolve_principal(hdb, key_and_id):
    raw, _ = key_and_id
    p = auth.resolve_principal(hdb, f"Bearer {raw}")
    assert p and p["scopes"] == {"read", "send"}
    assert auth.resolve_principal(hdb, "Bearer nope") is None
    assert auth.resolve_principal(hdb, None) is None


def test_check_scope_unauthenticated():
    token = auth.current_principal.set(None)
    try:
        p, err = auth.check_scope("read")
        assert p is None and err["error"] == "unauthenticated"
    finally:
        auth.current_principal.reset(token)


def test_check_scope_grant_deny_admin():
    token = auth.current_principal.set(
        {"token_id": 1, "mailbox_id": 1, "scopes": {"read"}, "active": True})
    try:
        assert auth.check_scope("read")[1] is None
        _, err = auth.check_scope("send")
        assert err["error"] == "forbidden" and "send" in err["detail"]
    finally:
        auth.current_principal.reset(token)
    token = auth.current_principal.set(
        {"token_id": 1, "mailbox_id": 1, "scopes": {"admin"}, "active": True})
    try:
        assert auth.check_scope("send")[1] is None       # admin implies all
    finally:
        auth.current_principal.reset(token)


# ---- middleware (raw ASGI) ----------------------------------------------------------

def _run_middleware(hdb, authorization):
    seen = {}

    async def inner_app(scope, receive, send):
        seen["principal"] = auth.current_principal.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = auth.MCPAuthMiddleware(inner_app, lambda: hdb)
    sent = []

    async def send(msg):
        sent.append(msg)

    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(mw(scope, None, send))
    status = sent[0]["status"]
    return status, seen.get("principal")


def test_middleware_valid_key_binds_principal(hdb, key_and_id):
    raw, tid = key_and_id
    status, principal = _run_middleware(hdb, f"Bearer {raw}")
    assert status == 200
    assert principal["token_id"] == tid and principal["scopes"] == {"read", "send"}
    assert auth.current_principal.get() is None          # reset after request


def test_middleware_missing_or_unknown_key_401(hdb):
    assert _run_middleware(hdb, None)[0] == 401
    assert _run_middleware(hdb, "Bearer wrong")[0] == 401


def test_middleware_paused_key_403(hdb, key_and_id):
    raw, tid = key_and_id
    db.set_auth_token_active(hdb, tid, False)
    assert _run_middleware(hdb, f"Bearer {raw}")[0] == 403
    db.set_auth_token_active(hdb, tid, True)
    assert _run_middleware(hdb, f"Bearer {raw}")[0] == 200