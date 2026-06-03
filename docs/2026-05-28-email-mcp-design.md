# Email MCP — Design Spec

**Date:** 2026-05-28
**Status:** Approved (design); implementation plan to follow
**Owner:** Swapnil

## 1. Goal

A local **MCP server** that gives AI agents safe, multi-account access to email over
**IMAP/SMTP**, working uniformly for **iCloud** and **Gmail** (both via app-specific
passwords). Designed for **multiple agents operating concurrently**, so all shared state
(cache, send-idempotency ledger, default account) lives in a single on-disk SQLite store.

Reuses the working IMAP code from
`../../job-trackers/job-application-email-tracker/mailconnector` — specifically the
`BODY.PEEK[]` + `readonly` fetch (never marks mail read), header decoding, plain-text
extraction, and nested-folder discovery. The Kafka/OTel/httpx poller (`main.py`) is **not**
reused.

### Non-goals (v1)
- Calendar / Reminders (separate spec after a CalDAV spike). **Notes is impossible via an
  app-specific password** — it has no IMAP/CalDAV API and would require local AppleScript on
  a Mac; explicitly out of scope.
- Reply/threading headers, retry/cancel of failed sends (data model anticipates the last
  one; behavior deferred).

> **Update (post-v1):** Attachments were since added — `get_emails` reports per-message
> attachment metadata, `download_attachment` saves one to a sandboxed download dir
> (read-only; untrusted filenames sanitized + realpath-confined), and `send_email` accepts
> an `attachments` list (local path or base64 content; 25 MB cap). See §4.9 / README.

## 2. Architecture

```
mcps/email/
  pyproject.toml
  server.py              # FastMCP entrypoint; registers the 8 tools
  accounts.py            # accounts.yml + Keychain resolution; default-account read/write
  textutil.py            # plain-text extraction, non-ASCII strip, token sizing/truncation
  providers/
    imap_account.py      # generalized IMAP: connect, fetch, flags, move, list_folders
    smtp_send.py         # SMTP send (STARTTLS, requireTLS)
  store.py               # SQLite (WAL): connection + schema + migrations
  cache.py               # 60-min TTL cache over store
  ledger.py              # send idempotency: 3 keys, status lifecycle, tags
  config/
    accounts.yml         # NON-SECRET: account list + which is default
  tests/                 # pytest; fake IMAP/SMTP doubles
```

### Credentials — macOS Keychain
- `config/accounts.yml` (non-secret, may be committed) lists accounts:
  ```yaml
  accounts:
    - name: icloud-personal
      email: you@icloud.com
      imap_host: imap.mail.me.com
      imap_port: 993
      smtp_host: smtp.mail.me.com
      smtp_port: 587
      default: true
    - name: gmail-personal
      email: you@gmail.com
      imap_host: imap.gmail.com
      imap_port: 993
      smtp_host: smtp.gmail.com
      smtp_port: 587
  ```
- The **app-specific password** is stored in the macOS Keychain via `keyring`, service
  `email-mcp`, username = account `name`. Never written to disk in plaintext, never logged,
  never returned by any tool.
- A small `setup` CLI (`python -m email_mcp.setup <account>`) prompts for the password
  (no echo) and writes it to Keychain.

### Connection model — connect-per-call
Each tool opens a fresh IMAP/SMTP connection and closes it (mirrors the existing code:
simple and robust). No pooling in v1. TLS is mandatory: IMAP over SSL (993); SMTP uses
STARTTLS with `requireTLS=True` and `tls.minVersion=TLSv1.2`. `rejectUnauthorized` is never
disabled.

## 3. SQLite schema (shared, WAL mode)

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);  -- holds 'default_account'

CREATE TABLE IF NOT EXISTS cache (
  key        TEXT PRIMARY KEY,   -- hash of (account, normalized filters, page params)
  payload    BLOB,               -- serialized result page(s)
  created_at TEXT                -- ISO8601 UTC; TTL = 60 min
);

CREATE TABLE IF NOT EXISTS send_ledger (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account         TEXT NOT NULL,
  key_kind        TEXT NOT NULL,  -- 'recipient' | 'recipient_subject' | 'recipient_body'
  key_hash        TEXT NOT NULL,  -- sha256 of the normalized key material
  recipients      TEXT NOT NULL,  -- sorted, comma-joined (for reporting)
  subject_excerpt TEXT,           -- first ~120 chars (for reporting)
  status          TEXT NOT NULL,  -- 'queued' | 'sent' | 'failed' | 'retried'
  tags            TEXT,           -- JSON array/object supplied by the agent
  sent_at         TEXT NOT NULL,  -- ISO8601 UTC
  message_id      TEXT            -- SMTP message-id once sent
);
CREATE INDEX IF NOT EXISTS idx_ledger_lookup ON send_ledger(key_kind, key_hash, sent_at);
```

The cache DB path defaults to `~/.local/state/email-mcp/state.db` (overridable via env).

## 4. Tools (8)

All tools accept an optional `account` (name); when omitted, the **default account** is used.

### 4.1 `list_accounts() -> [{name, email, default}]`
Reads `accounts.yml` + the persisted default. Never returns secrets.

### 4.2 `set_default_account(name) -> {default}`
Validates `name` exists in `accounts.yml`; writes `settings.default_account`. Persisted, so
all concurrent agents see the same default.

### 4.3 `get_emails(account?, filters?, query?, folders?, include_sent=false, strip_to_text=false, page=1, page_size=20) -> {emails, page, page_size, total_estimate, truncated, from_cache}`
The core read tool. **Search is folded in here.**
- **Filters are passed through as-is** to IMAP search criteria (the agent constructs IMAP
  search keys; the server forwards them). `query` is an additional free-text term applied
  **client-side across folders** (iCloud server-side search is weak).
- **Folders:** default selection = **INBOX + Junk/Spam** only. (Originally specified as "all
  folders except Sent/Drafts," but a full scan of every folder times out on large mailboxes —
  e.g. 65+ folders with connect-per-call — so the default was narrowed to INBOX+Junk on
  2026-05-28.) `include_sent=true` adds the Sent folder; `folders=[...]` overrides the
  selection entirely (use it to search any other folder).
- **Never marks read:** uses `BODY.PEEK[]` and `select(..., readonly=True)`.
- **Search window:** when `query`/date filters are absent, defaults to `SINCE` 90 days;
  agent can pass an explicit date filter to widen. (Rationale: bound iCloud load.)
- **Pagination & caching:** default `page_size=20`. By default (`cached=false`, added
  2026-05-28) get_emails fetches only the most-recent `page*page_size` per folder, in a
  single batched FETCH, **always fresh and uncached** — this keeps "latest N" fast and
  current (the original always-fetch-all behavior timed out on large mailboxes). Pass
  `cached=true` to compute the full result set once, cache it 60 min, and paginate from
  cache (stable paging over a fixed set). In uncached mode `total_estimate` reflects the
  fetched window, not the true match count.
- **Token cap:** the returned page is sized to **≤ ~100k tokens** (heuristic ≈ 4 chars /
  token). If bodies would exceed it, bodies are **truncated** (not dropped) and
  `truncated=true` is set; `total_estimate` still reflects the true match count. Never
  silently hide matches.
- **`strip_to_text`:** when true, return plain text with **non-ASCII stripped** (rich HTML
  reduced to pure text). When false, return the best plain-text extraction (HTML→text
  fallback via BeautifulSoup) without aggressive stripping.
- **Caching note:** cache key includes account + normalized filters + include_sent +
  strip_to_text. Cross-folder merged results are cached as one result set.

### 4.4 *(search)* — folded into `get_emails` via `query` + `folders`. No separate tool.

### 4.5 `send_email(account?, to, subject, body, tags?) -> {status, message_id} | BLOCKED{...}`
SMTP send with **idempotent dedup** (see §5). Records intent as `queued` before SMTP,
updates to `sent`/`failed`. Optional `tags` (JSON) is stored for agent bookkeeping.

### 4.6 `mark_email(account?, message_id, read: bool) -> {message_id, read}`
Sets/clears the `\Seen` flag via IMAP `STORE`. Must locate the message by `Message-ID`
(search across folders) since IMAP sequence numbers are per-folder/volatile.

### 4.7 `move_email(account?, message_id, dest_folder) -> {message_id, dest_folder}`
IMAP `MOVE` where supported; fallback `COPY` + set `\Deleted` + `EXPUNGE`. Validates
`dest_folder` against `list_folders`.

### 4.8 `list_folders(account?) -> [folder_name]`
Lists folders (including nested, via `imap.list()` parsing reused from `collect_emails.py`).
Needed for `move_email` targets and `folders` selection.

### 4.9 `download_attachment(message_id, filename?, index?, dest_dir?, account?, folders?, overwrite=false) -> {saved_path, ...}` *(post-v1)*
Locates a message by `Message-ID` (read-only; `BODY.PEEK[]`, never marks read), extracts the
selected attachment, and writes it to disk. Selection: explicit `index` or exact `filename`
(both surfaced by `get_emails`' per-message `attachments` metadata); a lone attachment needs
neither. Saved into `EMAIL_MCP_DOWNLOAD_DIR` (default `~/.local/state/email-mcp/attachments`)
unless `dest_dir` is given. **Security:** the email-supplied filename is untrusted, so it is
reduced to a bare component (`safe_filename`) and the resolved real path is verified to sit
directly inside the download dir — traversal (`../`, absolute, separators) cannot escape.
Existing files are preserved (auto `name (1).ext`) unless `overwrite=true`. Misses
(`not_found`, `no_attachments`, `attachment_not_selected` with the available list) are returned
as structured objects, never raised.

**`send_email` attachments (post-v1):** `send_email` gained an optional `attachments` list;
each item is `{path}` (read from disk) or `{content: base64, filename}` (+ optional
`mime_type`). Combined size is capped at 25 MB. The idempotency keys are unchanged (recipient/
subject/body), so attachments don't affect dedup.

## 9. Cache + server-side search (post-v1)

Added to make "latest emails" fast and to fix search returning false negatives:
- **In-memory message cache** (`mcache.py`): a thread-safe LRU of parsed emails (headers +
  extracted text + attachment metadata, never bytes), bounded by entry count AND a byte
  budget with per-body trimming, plus a per-folder "recent" index with a TTL. **Dual key** —
  Message-ID (immutable across moves) for content, `uid:folder:uidvalidity` fallback for mail
  with no/duplicate Message-ID. Process-wide singleton, built to also back a future shared
  HTTP server (`docs/TODO-http-shared-server.md`).
- **Prefetch poller** (`prefetch.py`): optional daemon thread (off by default;
  `EMAIL_MCP_PREFETCH_INTERVAL`) that warms the INBOX cache, delta-driven by UID, read-only.
- **`get_emails`**: a text `query` (and `from_address`/`subject`/`since`/raw criteria) now
  runs **server-side over the whole mailbox** (IMAP `TEXT`/`FROM`/…), not a client-side filter
  over the recent window — so matches outside the window are found. `searched_window_only`
  flags which mode ran. `body=false` returns headers-only; results carry `uid`/`uidvalidity`
  and are deduped by Message-ID. The latest-window default is served from the cache when warm.
- **`send_email`**: `allow_duplicate` (block only true repeats) and `idempotency_key`
  (caller-controlled) relax the recipient-only guard; the ledger records exactly the keys it
  checks so modes don't interfere.
- **`download_attachment`**: `download_all`, `return_base64` (small files inline), and
  `uid`+`folder` direct locate.

## 5. Send idempotency (detailed)

On each `send_email`, compute **three independent keys** from the normalized message:
1. `recipient`         = sha256(sorted recipients)                — **pure/hard block**
2. `recipient_subject` = sha256(sorted recipients + subject)
3. `recipient_body`    = sha256(sorted recipients + body)

**Block decision:** look up each key in `send_ledger` within the **last 10 minutes**. If
**any** key matches an existing row → **block** the send. The recipient-only key means a
second message to the same recipient set within the window is always blocked (this is the
baseline rule); the subject/body keys allow precise reporting and survive future relaxation
of the recipient-only rule.

**Blocked response** returns the prior matching record(s), not a bare error:
```json
{
  "status": "BLOCKED",
  "matched": ["recipient", "recipient_subject"],
  "reason": "duplicate send within 10 min window",
  "prior": {
    "sent_at": "2026-05-28T17:03:11Z",
    "status": "failed",          // queued | sent | failed | retried
    "recipients": "a@x.com,b@y.com",
    "tags": {"campaign": "followup-3"},
    "message_id": null
  }
}
```

**Status lifecycle:** a send writes `queued` rows (all three keys) *before* contacting SMTP
("sitting in local memory"), then updates the rows to `sent` (with `message_id`) on success
or `failed` on exception.

**Failed sends still block (v1).** Until the flow is proven, *any* prior attempt in the
window blocks — including a `failed` one. **Out of scope (anticipated):** later we will add
retry-of-failed and agent-initiated cancel-of-failed; the schema (`status`, per-row `id`)
already supports it.

## 6. Error handling
- All tools return structured results; errors are returned as `{error, detail}` objects, not
  raised through the transport. Credentials/secrets are never included in error text.
- IMAP/SMTP exceptions are caught per-call; connect-per-call means a failure never poisons a
  pooled connection.
- Folder-name encoding (IMAP modified UTF-7) handled centrally in `imap_account.py`.

## 7. Testing
- `pytest` with fake IMAP/SMTP doubles (no live iCloud in CI).
- Cover: never-mark-read (assert `PEEK`/`readonly`), pagination from cache, TTL expiry,
  token-cap truncation, non-ASCII strip, all three dedup keys + the failed-still-blocks rule,
  `move_email` fallback path, default-account persistence across "processes" (separate
  store connections).
- A separate, opt-in live smoke test (env-gated) against a real account for manual runs.

## 8. Security posture (lessons from the icloud-mcp audit)
- Secrets in Keychain only; never logged/returned.
- TLS enforced (`requireTLS`, no `rejectUnauthorized:false`).
- Inputs validated before use (folder names, message-ids, pagination bounds); no shell/eval.
- `send_email` is the one action sink — idempotency ledger is the guard against runaway/
  duplicate agent sends. (No auto-actions triggered by retrieved email content.)
- App-specific passwords are revocable from appleid.apple.com if anything misbehaves.
