"""Agent-key authentication for the MCP-over-HTTP surface.

Every request to the mounted MCP app must carry `Authorization: Bearer <agent key>`.
A PURE ASGI middleware (deliberately not Starlette's BaseHTTPMiddleware, which runs the
inner app in a separate task and would break ContextVar propagation) resolves the key to
a principal {token_id, mailbox_id, scopes} and binds it to `current_principal` for the
duration of the request; the MCP tools read it from there. Scope semantics:

- `read`  — list/fetch/search mail, download attachments
- `send`  — send_email
- `mint`  — create/list/pause agent keys for the same mailbox ("orchestrator" power)
- `admin` — implies everything, plus mailbox configuration

Keys are stored hashed (see http.db); a paused/deactivated key gets 403, an unknown or
missing key 401. Dashboard/REST routes authenticate separately (login tokens).
"""
import json
from contextvars import ContextVar

from email_mcp.http import db

current_principal = ContextVar("email_mcp_principal", default=None)


def parse_bearer(value):
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def resolve_principal(db_path, authorization):
    """Map an Authorization header to a principal dict, or None if unknown."""
    raw = parse_bearer(authorization)
    if raw is None:
        return None
    return db.principal_for_raw(db_path, raw)


def check_scope(scope):
    """(principal, None) when the bound principal holds `scope` (or admin); else
    (None, structured-error) for the tool to return verbatim."""
    p = current_principal.get()
    if p is None:
        return None, {"error": "unauthenticated",
                      "detail": "no agent key bound to this request"}
    if scope not in p["scopes"] and "admin" not in p["scopes"]:
        return None, {"error": "forbidden",
                      "detail": f"this agent key lacks the '{scope}' scope",
                      "scopes": sorted(p["scopes"])}
    return p, None


async def _reject(send, status, detail):
    body = json.dumps({"error": "unauthorized" if status == 401 else "forbidden",
                       "detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer")]})
    await send({"type": "http.response.body", "body": body})


class MCPAuthMiddleware:
    """Wraps the mounted MCP ASGI app: authenticates the agent key and binds the
    principal ContextVar in the SAME task that runs the request handler."""

    def __init__(self, app, db_path_fn):
        self.app = app
        self.db_path_fn = db_path_fn

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        principal = resolve_principal(
            self.db_path_fn(), (headers.get(b"authorization") or b"").decode("latin-1"))
        if principal is None:
            return await _reject(send, 401, "missing or unknown agent key")
        if not principal["active"]:
            return await _reject(send, 403, "this agent key is paused or deactivated")
        token = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(token)
