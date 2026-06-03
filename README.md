# email-mcp

Local MCP server for multi-account IMAP/SMTP email (iCloud + Gmail via app-specific
passwords). Never marks mail read. Cross-folder search, idempotent sends, TLS verified.

## Status

- **Tested against iCloud only.** The design is provider-generic (Gmail config is
  included as an example), but **only iCloud has been exercised end-to-end** so far.
  Gmail/others are untested — use at your own risk and please report back.
- **macOS only** (uses the macOS Keychain via `keyring`).

## Requirements

- An **app-specific password** for each mailbox, stored in your **macOS Keychain**
  (see [Configure](#configure) / [Get an app-specific password](#get-an-app-specific-password)).
  The password is read from the Keychain at runtime — it is never written to disk,
  logged, or returned by any tool.
- **You must grant Keychain permission.** The first time the server reads the password,
  macOS shows a dialog: *"… wants to use your confidential information stored in
  'email-mcp' in your keychain."* — click **Allow** (or **Always Allow**). This grants
  access to **only that one `email-mcp` item**, nothing else in your Keychain. Note the
  prompt is tied to the specific Python binary running the server, so it may re-ask if
  you switch interpreters.

## Install
```bash
pip install email-mcp        # or: pipx install email-mcp
```
This provides an `email-mcp` command (and `python -m email_mcp.server`).

## Configure
1. Create your account registry at `~/.config/email-mcp/accounts.yml` (see
   `config/accounts.example.yml` for the format). Override the location with the
   `EMAIL_MCP_ACCOUNTS` env var if you prefer.
2. Store each account's app-specific password in the macOS Keychain:
   ```bash
   python -m email_mcp.setup_cli icloud-personal
   ```
   (For local development you can instead set `EMAIL_MCP_PASSWORD` in a `.env`.)

## Register with Claude Code
```bash
claude mcp add email --scope user -- email-mcp
```

## Tools
list_accounts, set_default_account, list_folders, get_emails (recency by default;
search via query/filters; per-message `attachments` metadata), download_attachment,
send_email (idempotent; optional `attachments`), mark_email, move_email.

### Searching & speed
- **`get_emails` has two modes**, reported by `searched_window_only` in the result:
  - *No search terms* → fast: the most-recent `page*page_size` per folder, served from
    an in-memory cache when warm (a repeat read is ~instant). `searched_window_only=true`
    here, so an empty result means "not in the recent window", not "doesn't exist".
    Pass `fresh=true` to force a live read.
  - *Search* (`query`, `from_address`, `subject`, `since=YYYY-MM-DD`, `has_attachment`,
    or raw `filters.criteria`) → runs **server-side over the whole mailbox** — `query` is
    a full-text IMAP search, so matches outside the recent window are found.
    `searched_window_only=false`.
- **`body=false`** omits message bodies (cheap headers + attachment metadata) — ideal
  for *finding* a message before opening it. Each message also carries `uid`/`uidvalidity`
  for robust follow-up actions.
- **Background prefetch** keeps the latest INBOX warm. Off by default; enable with
  `EMAIL_MCP_PREFETCH_INTERVAL=120` (seconds; delta-fetches only new mail by UID, never
  marks read). Cache size knobs: `EMAIL_MCP_CACHE_ENTRIES` (256),
  `EMAIL_MCP_CACHE_BYTES` (32MiB), `EMAIL_MCP_CACHE_BODY_MAX` (64KiB),
  `EMAIL_MCP_CACHE_RECENT_TTL` (180s).
- **`send_email`** dedup: by default a 2nd mail to the same recipients within 10 min is
  BLOCKED. `allow_duplicate=true` relaxes this to block only a true repeat (same
  recipients AND subject/body); `idempotency_key` gives caller-controlled dedup.

> Roadmap: running this as one shared HTTP server so a whole fleet of agents share a
> single warm cache + prefetch — see `docs/TODO-http-shared-server.md`.

### Attachments
- **Reading:** `get_emails` reports an `attachments` list per message
  (`{index, filename, mime_type, size, inline}`) — metadata only, never the bytes.
  `download_attachment(message_id, filename=… | index=…)` writes one attachment to
  disk (read-only; never marks mail read) and returns the saved path. `download_all=true`
  saves every attachment; `return_base64=true` returns small files (≤256KB) inline
  instead of to disk; `uid`+`folder` (from get_emails) locate a message with no/duplicate
  Message-ID. Files land in `EMAIL_MCP_DOWNLOAD_DIR`
  (default `~/.local/state/email-mcp/attachments`) unless you pass `dest_dir`. The
  email-supplied filename is sanitized and confined to that directory (path-traversal
  safe); existing files are not clobbered unless `overwrite=true`.
- **Sending:** `send_email`'s optional `attachments` is a list where each item is
  either `{"path": "/local/file"}` (read from disk) or `{"content": "<base64>",
  "filename": "name.ext"}`, with an optional `"mime_type"`. Combined size is capped at
  25 MB.

## Get an app-specific password
- iCloud: appleid.apple.com -> Sign-In & Security -> App-Specific Passwords.
- Gmail: myaccount.google.com -> Security -> App passwords.

## Develop
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
