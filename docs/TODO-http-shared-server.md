# TODO — HTTP transport: one shared server for many agents

**Status:** Parked / roadmap. **Filed:** 2026-06-02.
**Motivation:** make the in-memory message cache + prefetch a *shared* resource.

## Why

Today `email-mcp` runs as a **stdio** server: every client (each interactive Claude
Code session, each `claude -p` watchdog run) spawns its **own** process, so each gets
its **own** cold cache and — if enabled — its **own** prefetch thread hammering iCloud.
That's wasteful and means the cache never warms across agents.

Running it instead as a **long-lived HTTP server** flips this: one process, **one warm
cache, one prefetch thread**, shared by every agent on the box (and potentially the
whole tailnet fleet — cherry/blackberry/blueberry). This is the natural payoff of the
message cache added in the same change as this note — the cache was built process-wide
and thread-safe precisely so it can back a shared server with no redesign.

## What to build

- **Transport:** MCP **streamable-http** (preferred; falls back to SSE). FastMCP
  already supports it — `mcp.run(transport="streamable-http")` with host/port — so the
  tool layer needs no changes. Gate on an env/flag, e.g. `EMAIL_MCP_TRANSPORT=http`
  + `EMAIL_MCP_HTTP_HOST`/`EMAIL_MCP_HTTP_PORT` (default `127.0.0.1:8765`).
- **Lifecycle:** run as a scheduled/long-lived service (mirror the launchlab task
  pattern, hidden-launch.vbs). Single instance. Prefetch interval makes real sense here
  (one poller, not N).
- **Registration:** point clients at the URL instead of a command, e.g.
  `claude mcp add email --transport http http://127.0.0.1:8765/mcp`. The watchdog's
  `mcp-config.json` switches from `command` to `url`.

## Decisions to make first

1. **Auth.** Even on `127.0.0.1` / tailnet-only, add a bearer token
   (`EMAIL_MCP_HTTP_TOKEN`) so any local process can't read mail. If exposed beyond
   localhost, it MUST be behind the nginx TLS proxy + token, never raw.
2. **Multi-account isolation.** The cache is currently keyed per-folder, single default
   account. A shared server used by agents acting on different accounts needs the cache
   + prefetch keyed by **account name** (cache already stores per-entry account context;
   extend `recent` index and prefetch to be per-account).
3. **Concurrency / connection model.** Connect-per-call IMAP is fine for stdio bursts;
   under a shared HTTP server with many concurrent agents it may warrant a small **IMAP
   connection pool** per account (bounded), since iCloud limits simultaneous
   connections. Measure before adding.
4. **Send idempotency ledger** is already SQLite-backed (WAL, shared) — it already works
   across processes, so it transparently covers multiple agents hitting one server.
5. **Cache invalidation on mutate.** When any agent calls `move_email`/`mark_email`, the
   shared cache entry/recent index should be invalidated so other agents don't see stale
   state. Add a cache `invalidate(message_id|folder)` hook wired into the mutate paths.

## Non-goals (v1 of HTTP mode)

- Horizontal scaling / multiple server instances (one box, one process is enough for the
  fleet). No distributed cache.
- Per-agent rate limiting beyond what iCloud already enforces.
