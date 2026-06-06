# The HTTP service — multi-tenant email for an agent fleet

Fetch this guide any time: `GET <base-url>/help` (markdown) · machine-readable
summary: `GET <base-url>/info` · in the repo/package: `email_mcp/http/HELP.md`.

The second surface of email-mcp (the first is the stdio MCP server, which is unchanged
and shares no state with this one): a long-lived FastAPI app that many agents use over
HTTPS, each through its own scoped key, with humans onboarding via a Matrix bot and a
web dashboard. Everything lives under `src/email_mcp/http/`; the core IMAP/SMTP logic
(`email_ops`, `security`, providers) is reused unchanged.

```
pip install "email-mcp[http]"     # fastapi, uvicorn, jinja2, cryptography, httpx
email-mcp-http                    # binds EMAIL_MCP_HTTP_HOST:EMAIL_MCP_HTTP_PORT
```

## Surfaces

| Path | What | Auth |
|---|---|---|
| `/mcp` | MCP over streamable-HTTP (stateless; the 8 email tools) | `Authorization: Bearer <agent key>` |
| `/api/*` | REST: setup, key mint/pause/revoke, usage, approvals | login token (Bearer) or agent key |
| `/` | Human dashboard (mailbox config, keys, usage, policy) | session cookie (from the sign-in link) |
| `/info` | Machine-readable how-to (agents can fetch and self-configure) | none |
| `/health` | Liveness + config sanity | none |

## Identity & auth model

- **Users** are Matrix IDs. The service's bot self-registers (`@emailer:…`, suffixed on
  collision) and persists its own creds in the DB (`service_identity`).
- **Onboarding**: DM the bot `login` → a 24h **single-use** sign-in link. Opening it
  consumes the token and swaps it for a separately-minted session cookie; until opened,
  the token also works as a REST Bearer credential. `logout` (DM) erases stored mailbox
  passwords, ends dashboard sessions, and voids unredeemed links.
- **Agent keys** (`auth_tokens`) are bound to one mailbox, carry scopes
  (`read`/`write`/`send`/`mint`/`admin`), are stored hashed, shown once, and metered
  (reads/searches/sends/blocked). `mint`-scoped keys can create subagent keys limited
  to scopes they themselves hold.
- **Mailbox passwords** are AES-GCM encrypted with a key HKDF-derived from
  `EMAIL_MCP_MASTER_KEY` + a per-row salt.

## Send policy: three tiers + owner approval

Per-mailbox `policy_json` (same regex semantics as the stdio `security:` section, plus
`blocked_recipients`):

1. `blocked_recipients` match → **BLOCKED** (always wins — even over a later 👍).
2. `allowed_recipients` match, or no allowlist at all → **sent** immediately.
3. Anything else → **pending_approval**: the owner gets a Matrix DM preview and
   `EMAIL_MCP_APPROVAL_TTL` (default 180) seconds to react. 👍 performs the real send
   (the dedup ledger still applies); any other reaction rejects; silence expires it.
   The agent polls the `get_send_status` tool or `GET /api/approvals/{id}`.

Without a configured bot the approve tier degrades to a plain deny — stdio semantics.

## Matrix bot privacy rules

Commands are DM-only, and "DM" is verified **at every send** (`joined_members` must be
at most the bot + the addressee) — never from an invite-time cache, so a room that
later gains a third member stops receiving sign-in links and previews; notifications
abandon such a room and mint a fresh private one.

## Env vars

| Var | Default | Meaning |
|---|---|---|
| `EMAIL_MCP_DB` | `~/.local/state/email-mcp/state.db` | SQLite state (WAL) |
| `EMAIL_MCP_MASTER_KEY` | — (required for passwords) | encryption master key |
| `EMAIL_MCP_BASE_URL` | `https://email-mcp.surdi.in` | public URL used in links |
| `EMAIL_MCP_HTTP_HOST` / `_PORT` | `127.0.0.1` / `8765` | bind address |
| `EMAIL_MCP_MATRIX_URL` | — (bot off) | homeserver, e.g. `http://matrix-synapse:8008` |
| `EMAIL_MCP_MATRIX_USERNAME` | `emailer` | bot localpart base |
| `EMAIL_MCP_APPROVAL_TTL` | `180` | seconds the owner has to 👍 a send |
| `EMAIL_MCP_STATUS_URL` / `_TOKEN` / `_ID` | — (reporter off) | fleet status hub heartbeat |

## Operations

- Register an MCP client:
  `claude mcp add email --transport http <base>/mcp --header "Authorization: Bearer <key>"`
- Break-glass login link (bot down):
  `python -m email_mcp.http.admin login-token @user:server [--ttl 86400]`
- The deployment recipe (Docker, nginx, compose) lives in the launchlab repo; the
  container simply runs `email-mcp-http` with the env above.
