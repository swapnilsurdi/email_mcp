"""Machine-readable service description (`GET /info`) so an agent can web-fetch how to
set itself up, and `GET /health` for the status reporter / humans."""
from email_mcp.http import crypto

VERSION = "0.2.0-dev"

SCOPES = {
    "read": "fetch/search emails, list folders, download attachments",
    "write": "mark read/unread, move between folders",
    "send": "send email (subject to the mailbox's recipient policy)",
    "mint": "create/list/pause agent keys for the same mailbox (orchestrators)",
    "admin": "implies everything, plus mailbox configuration",
}


def service_info(base_url, bot=None, approval_ttl=180):
    return {
        "service": "email-mcp",
        "version": VERSION,
        "description": (
            "Multi-tenant IMAP/SMTP email for agents: MCP over streamable-HTTP, "
            "gated by scoped agent keys, with a web dashboard and Matrix-driven "
            "onboarding."),
        "matrix_bot": bot,
        "docs": f"{base_url}/help",
        "mcp": {
            "endpoint": f"{base_url}/mcp",
            "transport": "streamable-http",
            "auth": "Authorization: Bearer <agent key> on every request",
            "register_with_claude": (
                f"claude mcp add email --transport http {base_url}/mcp "
                f"--header \"Authorization: Bearer <agent key>\""),
            "tools": ["list_accounts", "list_folders", "get_emails",
                      "download_attachment", "mark_email", "move_email", "send_email",
                      "get_send_status"],
        },
        "send_policy": {
            "tiers": "blocked_recipients -> BLOCKED; allowed_recipients match (or no "
                     "allowlist) -> sent; anything else -> pending_approval",
            "approval": f"the mailbox owner gets a Matrix DM preview and "
                        f"{approval_ttl}s to react 👍 (anything else / timeout "
                        "rejects); poll get_send_status or GET /api/approvals/{id} "
                        "for the outcome",
        },
        "scopes": SCOPES,
        "setup_flow": [
            f"1. DM the service's Matrix bot ({bot or 'see the landing page'}) "
            "and send `login` — it replies with a 24h single-use dashboard link "
            "whose token also works as a REST Bearer credential until the link "
            "is opened.",
            "2. Open the dashboard link (or use the REST API with `Authorization: "
            "Bearer <login token>`) to connect a mailbox: email + IMAP/SMTP hosts + "
            "app-specific password (stored encrypted) + recipient policy.",
            "3. Create agent keys with the scopes each agent needs; each key is shown "
            "once. Put it in the agent's MCP config as a Bearer header.",
            "4. Orchestrator keys (scope `mint`) can create further task-scoped "
            "subagent keys via POST /api/tokens.",
        ],
        "api": {
            "GET /info": {"auth": "none", "returns": "this document"},
            "GET /help": {"auth": "none",
                          "returns": "the full service guide (markdown)"},
            "GET /health": {"auth": "none", "returns": "liveness + config sanity"},
            "POST /api/setup": {
                "auth": "Bearer <login token>",
                "body": {"mailbox": {"name": "str?", "email": "str",
                                     "imap_host": "str", "imap_port": 993,
                                     "smtp_host": "str", "smtp_port": 587,
                                     "password": "app-specific password"},
                         "policy": {"allowed_recipients": ["regex..."],
                                    "blocked_recipients": ["regex..."],
                                    "protected_folders": ["regex..."],
                                    "readable_folders": "[regex...] | null",
                                    "blocked_folders": ["regex..."],
                                    "protect_trash": True}},
                "returns": "{mailbox_id, email} — one active mailbox per email "
                           "address across the whole system",
            },
            "GET /api/mailboxes": {"auth": "Bearer <login token>"},
            "POST /api/tokens": {
                "auth": "Bearer <login token> | agent key with scope `mint`",
                "body": {"mailbox_id": "int (login-token auth only; a mint key is "
                                       "bound to its own mailbox)",
                         "label": "str", "scopes": "csv or list, e.g. 'read,send'"},
                "returns": "{token} — SHOWN ONCE, store it now",
                "note": "a mint key can only grant scopes it itself holds "
                        "(minus mint/admin)",
            },
            "GET /api/tokens": {
                "auth": "Bearer <login token> | agent key with scope `mint`",
                "returns": "keys with usage counters (reads/searches/sends/blocked)"},
            "PATCH /api/tokens/{id}": {"auth": "same as GET",
                                       "body": {"active": "bool (pause/resume)"}},
            "DELETE /api/tokens/{id}": {"auth": "same as GET",
                                        "effect": "permanent revocation"},
            "DELETE /api/mailboxes/{id}": {
                "auth": "Bearer <login token>",
                "effect": "delete account: erases the stored password and tombstones "
                          "the email so it can be registered again"},
            "POST /api/mailboxes/{id}/disconnect": {
                "auth": "Bearer <login token>",
                "effect": "logout: erases the stored password; agents get "
                          "mailbox_unavailable until reconnected"},
            "GET /api/approvals/{id}": {
                "auth": "Bearer <login token> | agent key of the same mailbox",
                "returns": "{approval_id, status: pending|approved|rejected|expired, "
                           "recipients, subject, result}"},
        },
        "dashboard": f"{base_url}/?token=<login token>",
    }


def health(db_ok):
    return {"ok": bool(db_ok),
            "version": VERSION,
            "db": "ok" if db_ok else "error",
            "master_key_configured": crypto.master_key_configured()}
