"""The email-mcp HTTP service: one FastAPI app serving
- `/mcp`     — MCP streamable-http (stateless), gated per-request by agent keys
- `/api/*`   — REST for setup/keys/usage (login-token or mint-key auth)
- `/`        — the human dashboard (login-token session cookie)
- `/info`, `/health` — public discovery + liveness

Run: `email-mcp-http` (console script) or `python -m email_mcp.http.app`.
Env: EMAIL_MCP_DB, EMAIL_MCP_MASTER_KEY, EMAIL_MCP_HTTP_HOST/PORT,
EMAIL_MCP_BASE_URL (public URL used in links, default https://email-mcp.surdi.in).
"""
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from email_mcp import runtime
from email_mcp.http import auth, crypto, db, info, tools

SESSION_COOKIE = "emcp_session"
GRANTABLE_BY_MINT = {"read", "write", "send"}   # a mint key can never grant mint/admin

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _base_url():
    return os.environ.get("EMAIL_MCP_BASE_URL", "https://email-mcp.surdi.in").rstrip("/")


def _master_key():
    return os.environ.get("EMAIL_MCP_MASTER_KEY")


# ---- request auth helpers ------------------------------------------------------------

def _login_user(request, db_path, now=None):
    """The dashboard/API human: a session cookie (minted on login-link redemption) or an
    unredeemed 24h login token as a Bearer header. Returns the user row or None."""
    now = now or time.time()
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        user_id = db.validate_session(db_path, raw, now)
        if user_id:
            return db.get_user(db_path, user_id)
    raw = auth.parse_bearer(request.headers.get("authorization", ""))
    if not raw:
        return None
    user_id = db.validate_login_token(db_path, raw, now)
    return db.get_user(db_path, user_id) if user_id else None


def _mint_principal(request, db_path):
    """An agent key with the `mint` (or admin) scope, for programmatic key management."""
    p = auth.resolve_principal(db_path, request.headers.get("authorization", ""))
    if p and p["active"] and ({"mint", "admin"} & p["scopes"]):
        return p
    return None


def _user_mailbox_ids(db_path, user_id):
    return [m["id"] for m in db.list_mailboxes_for_user(db_path, user_id)]


def _token_view(t):
    return {k: t[k] for k in ("id", "mailbox_id", "label", "scopes", "prefix",
                              "active", "revoked", "created_at", "last_used_at",
                              "cnt_read", "cnt_search", "cnt_send", "cnt_blocked")}


def _parse_scopes(scopes):
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",")]
    return [s for s in (scopes or []) if s]


async def _json_body(request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON")


class _McpPathShim:
    """Rewrite the exact path /mcp to /mcp/ so the mounted MCP app is hit directly
    instead of via a 307 redirect (pure ASGI; runs before routing)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


# ---- app factory -----------------------------------------------------------------------

def create_app(db_path_fn=None, master_key_fn=None, base_url=None,
               mount_mcp=True):
    db_path_fn = db_path_fn or runtime.db_path
    master_key_fn = master_key_fn or _master_key
    base = base_url or _base_url()

    mcp = tools.build_mcp(db_path_fn, master_key_fn) if mount_mcp else None

    @asynccontextmanager
    async def lifespan(app):
        db.init_http_tables(db_path_fn())
        if mcp is not None:
            async with mcp.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="email-mcp", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def referrer_policy(request, call_next):
        # login links carry a secret in the query string; never let it ride a Referer
        resp = await call_next(request)
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    if mcp is not None:
        app.mount("/mcp", auth.MCPAuthMiddleware(mcp.streamable_http_app(), db_path_fn))
        # Starlette redirects a bare POST /mcp -> /mcp/ (307). MCP clients shouldn't
        # need a redirect round-trip (and some won't follow it), so normalize the path
        # before routing.
        app.add_middleware(_McpPathShim)

    # ---- public ----------------------------------------------------------------------

    @app.get("/info")
    def get_info():
        return info.service_info(base)

    @app.get("/health")
    def get_health():
        try:
            db.init_http_tables(db_path_fn())
            ok = True
        except Exception:
            ok = False
        return info.health(ok)

    # ---- REST: setup ------------------------------------------------------------------

    def _require_login(request):
        user = _login_user(request, db_path_fn())
        if user is None:
            raise HTTPException(401, "a valid login token is required "
                                     "(DM the Matrix bot and send `login`)")
        return user

    @app.post("/api/setup")
    async def api_setup(request: Request):
        user = _require_login(request)
        body = await _json_body(request)
        mb = body.get("mailbox") or {}
        policy = body.get("policy") or {}
        email = (mb.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise HTTPException(422, "mailbox.email is required")
        existing = next((m for m in db.list_mailboxes_for_user(db_path_fn(), user["id"])
                         if m["email"] == email), None)
        now = time.time()
        if existing:
            mid = existing["id"]
            with_fields = {k: mb[k] for k in
                           ("name", "imap_host", "imap_port", "smtp_host", "smtp_port")
                           if k in mb}
            if with_fields:
                from email_mcp import store
                sets = ", ".join(f"{k}=?" for k in with_fields)
                with store.connect(db_path_fn()) as conn:
                    conn.execute(f"UPDATE mailboxes SET {sets} WHERE id=?",
                                 (*with_fields.values(), mid))
            if mb.get("password"):
                db.set_mailbox_password(db_path_fn(), mid, mb["password"],
                                        master_key=master_key_fn())
            if "policy" in body:
                db.set_mailbox_policy(db_path_fn(), mid, policy)
            return {"mailbox_id": mid, "email": email, "updated": True}
        try:
            mid = db.create_mailbox(
                db_path_fn(), user["id"], mb.get("name"), email,
                mb.get("imap_host"), int(mb.get("imap_port") or 993),
                mb.get("smtp_host"), int(mb.get("smtp_port") or 587),
                mb.get("password") or "", policy=policy, now=now,
                master_key=master_key_fn())
        except db.EmailTaken:
            raise HTTPException(409, f"{email} is already registered to a mailbox; "
                                     "one active mailbox per email across the system")
        except crypto.MasterKeyMissing as e:
            raise HTTPException(500, str(e))
        return {"mailbox_id": mid, "email": email, "created": True}

    @app.get("/api/mailboxes")
    def api_mailboxes(request: Request):
        user = _require_login(request)
        out = []
        for m in db.list_mailboxes_for_user(db_path_fn(), user["id"]):
            out.append({"id": m["id"], "name": m["name"], "email": m["email"],
                        "imap_host": m["imap_host"], "imap_port": m["imap_port"],
                        "smtp_host": m["smtp_host"], "smtp_port": m["smtp_port"],
                        "connected": bool(m["enc_password"]),
                        "policy": db.mailbox_policy_dict(db_path_fn(), m["id"])})
        return out

    @app.post("/api/mailboxes/{mid}/disconnect")
    def api_disconnect(mid: int, request: Request):
        user = _require_login(request)
        if mid not in _user_mailbox_ids(db_path_fn(), user["id"]):
            raise HTTPException(404, "no such mailbox")
        db.erase_mailbox_password(db_path_fn(), mid)
        return {"mailbox_id": mid, "disconnected": True}

    @app.delete("/api/mailboxes/{mid}")
    def api_delete_mailbox(mid: int, request: Request):
        user = _require_login(request)
        if mid not in _user_mailbox_ids(db_path_fn(), user["id"]):
            raise HTTPException(404, "no such mailbox")
        db.delete_mailbox(db_path_fn(), mid, now=time.time())
        return {"mailbox_id": mid, "deleted": True}

    # ---- REST: agent keys ---------------------------------------------------------------

    @app.post("/api/tokens")
    async def api_create_token(request: Request):
        body = await _json_body(request)
        label = (body.get("label") or "").strip() or "unnamed"
        scopes = _parse_scopes(body.get("scopes"))
        if not scopes:
            raise HTTPException(422, "scopes is required, e.g. ['read','send']")
        db_path = db_path_fn()
        user = _login_user(request, db_path)
        now = time.time()
        if user is not None:
            mid = body.get("mailbox_id")
            mids = _user_mailbox_ids(db_path, user["id"])
            if mid is None and len(mids) == 1:
                mid = mids[0]
            if mid not in mids:
                raise HTTPException(404, "mailbox_id missing or not yours")
            created_by = user["matrix_user"]
        else:
            minter = _mint_principal(request, db_path)
            if minter is None:
                raise HTTPException(401, "login token or a mint-scoped agent key required")
            illegal = set(scopes) - GRANTABLE_BY_MINT
            if illegal and "admin" not in minter["scopes"]:
                raise HTTPException(403, f"a mint key cannot grant {sorted(illegal)}")
            not_held = set(scopes) - minter["scopes"] - {"read"}
            if not_held and "admin" not in minter["scopes"]:
                raise HTTPException(403, f"cannot grant scopes the minting key lacks: "
                                         f"{sorted(not_held)}")
            mid = minter["mailbox_id"]
            created_by = f"token:{minter['token_id']}"
        try:
            raw, tid = db.create_auth_token(db_path, mid, label, scopes, created_by, now)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"token": raw, "token_id": tid, "mailbox_id": mid,
                "scopes": scopes, "note": "shown once — store it now"}

    def _tokens_scope(request):
        """(db_path, mailbox_ids) the caller may inspect."""
        db_path = db_path_fn()
        user = _login_user(request, db_path)
        if user is not None:
            return db_path, _user_mailbox_ids(db_path, user["id"])
        minter = _mint_principal(request, db_path)
        if minter is not None:
            return db_path, [minter["mailbox_id"]]
        raise HTTPException(401, "login token or a mint-scoped agent key required")

    @app.get("/api/tokens")
    def api_list_tokens(request: Request):
        db_path, mids = _tokens_scope(request)
        out = []
        for mid in mids:
            out.extend(_token_view(t) for t in db.list_auth_tokens(db_path, mid))
        return out

    @app.patch("/api/tokens/{tid}")
    async def api_patch_token(tid: int, request: Request):
        db_path, mids = _tokens_scope(request)
        t = db.get_auth_token(db_path, tid)
        if t is None or t["mailbox_id"] not in mids:
            raise HTTPException(404, "no such key")
        body = await _json_body(request)
        if "active" in body:
            db.set_auth_token_active(db_path, tid, bool(body["active"]))
        return _token_view(db.get_auth_token(db_path, tid))

    @app.delete("/api/tokens/{tid}")
    def api_revoke_token(tid: int, request: Request):
        db_path, mids = _tokens_scope(request)
        t = db.get_auth_token(db_path, tid)
        if t is None or t["mailbox_id"] not in mids:
            raise HTTPException(404, "no such key")
        db.revoke_auth_token(db_path, tid)
        return {"token_id": tid, "revoked": True}

    # ---- dashboard ------------------------------------------------------------------------

    def _home(request, user, flash=None, flash_error=False, new_key=None):
        db_path = db_path_fn()
        mailboxes = db.list_mailboxes_for_user(db_path, user["id"])
        tokens = []
        for m in mailboxes:
            tokens.extend(db.list_auth_tokens(db_path, m["id"]))
        policy = db.mailbox_policy_dict(db_path, mailboxes[0]["id"]) if mailboxes else {}
        return _templates.TemplateResponse(request, "home.html", {
            "user": user, "mailboxes": mailboxes, "tokens": tokens, "policy": policy,
            "flash": flash, "flash_error": flash_error, "new_key": new_key,
            "base_url": base})

    @app.get("/", response_class=HTMLResponse)
    def dash_root(request: Request, token: str = None, flash: str = None):
        db_path = db_path_fn()
        if token:
            # single-use redemption: the URL value is consumed here and exchanged for a
            # freshly-minted session value, so it can't be replayed from logs/history
            now = time.time()
            uid = db.consume_login_token(db_path, token, now)
            if uid is None:
                return _templates.TemplateResponse(request, "landing.html", {
                    "user": None, "error": "That sign-in link is invalid or expired — "
                                           "send `login` to the bot again.",
                    "bot": db.get_service_identity(db_path, "matrix_user")})
            session = db.create_session(db_path, uid, now)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(SESSION_COOKIE, session, max_age=86400, httponly=True,
                            samesite="lax", secure=base.startswith("https"))
            return resp
        user = _login_user(request, db_path)
        if user is None:
            return _templates.TemplateResponse(request, "landing.html", {
                "user": None, "error": None,
                "bot": db.get_service_identity(db_path, "matrix_user")})
        return _home(request, user, flash=flash)

    def _check_same_origin(request):
        """The dash forms are cookie-authenticated, so reject any browser-attributed
        cross-origin POST (Origin, falling back to Referer). Layered on the cookie's
        samesite=lax; requests without either header (CLIs, tests) pass through."""
        src = request.headers.get("origin") or request.headers.get("referer")
        if not src:
            return
        b, s = urlsplit(base), urlsplit(src)
        if (s.scheme, s.netloc) != (b.scheme, b.netloc):
            raise HTTPException(403, "cross-origin form submission rejected")

    def _dash_user(request):
        _check_same_origin(request)
        user = _login_user(request, db_path_fn())
        if user is None:
            raise HTTPException(401, "session expired — send `login` to the bot again")
        return user

    @app.post("/dash/signout")
    def dash_signout(request: Request):
        _check_same_origin(request)
        raw = request.cookies.get(SESSION_COOKIE)
        if raw:
            db.delete_session(db_path_fn(), raw)
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.post("/dash/mailbox")
    async def dash_mailbox(request: Request):
        user = _dash_user(request)
        form = await request.form()
        policy = {}
        for key in ("allowed_recipients", "blocked_recipients"):
            vals = [s.strip() for s in (form.get(key) or "").split(",") if s.strip()]
            if vals:
                policy[key] = vals
        body = {"mailbox": {k: form.get(k) for k in
                            ("email", "password", "imap_host", "imap_port",
                             "smtp_host", "smtp_port")},
                "policy": policy}

        # reuse the API handler logic by faking its body
        class _Req:
            cookies = request.cookies
            headers = request.headers

            async def json(self):
                return body
        try:
            await api_setup(_Req())
        except HTTPException as e:
            return _home(request, user, flash=e.detail, flash_error=True)
        return _home(request, user, flash="Mailbox saved.")

    @app.post("/dash/tokens")
    async def dash_tokens(request: Request):
        user = _dash_user(request)
        form = await request.form()
        scopes = form.getlist("scopes") or ["read"]
        db_path = db_path_fn()
        mids = _user_mailbox_ids(db_path, user["id"])
        if not mids:
            return _home(request, user, flash="Connect a mailbox first.",
                         flash_error=True)
        raw, _tid = db.create_auth_token(db_path, mids[0],
                                         form.get("label") or "unnamed",
                                         scopes, user["matrix_user"], time.time())
        return _home(request, user, flash="Key created.", new_key=raw)

    @app.post("/dash/tokens/{tid}/toggle")
    def dash_token_toggle(tid: int, request: Request):
        user = _dash_user(request)
        db_path = db_path_fn()
        t = db.get_auth_token(db_path, tid)
        if t and t["mailbox_id"] in _user_mailbox_ids(db_path, user["id"]):
            db.set_auth_token_active(db_path, tid, not t["active"])
        return RedirectResponse("/", status_code=303)

    @app.post("/dash/tokens/{tid}/revoke")
    def dash_token_revoke(tid: int, request: Request):
        user = _dash_user(request)
        db_path = db_path_fn()
        t = db.get_auth_token(db_path, tid)
        if t and t["mailbox_id"] in _user_mailbox_ids(db_path, user["id"]):
            db.revoke_auth_token(db_path, tid)
        return RedirectResponse("/", status_code=303)

    @app.post("/dash/mailboxes/{mid}/disconnect")
    def dash_mb_disconnect(mid: int, request: Request):
        user = _dash_user(request)
        if mid in _user_mailbox_ids(db_path_fn(), user["id"]):
            db.erase_mailbox_password(db_path_fn(), mid)
        return RedirectResponse("/?flash=Mailbox+disconnected+—+stored+password+erased.",
                                status_code=303)

    @app.post("/dash/mailboxes/{mid}/delete")
    def dash_mb_delete(mid: int, request: Request):
        user = _dash_user(request)
        if mid in _user_mailbox_ids(db_path_fn(), user["id"]):
            db.delete_mailbox(db_path_fn(), mid, now=time.time())
        return RedirectResponse("/?flash=Mailbox+deleted+—+the+email+is+freed.",
                                status_code=303)

    @app.exception_handler(HTTPException)
    async def http_exc(request, exc):
        if request.url.path.startswith(("/api/", "/mcp")):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return _templates.TemplateResponse(request, "landing.html", {
                "user": None, "error": exc.detail,
                "bot": db.get_service_identity(db_path_fn(), "matrix_user")},
                status_code=401)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def main():
    import uvicorn
    runtime.load_dotenv()
    host = os.environ.get("EMAIL_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("EMAIL_MCP_HTTP_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
