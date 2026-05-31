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
search via query/filters), send_email (idempotent), mark_email, move_email.

## Get an app-specific password
- iCloud: appleid.apple.com -> Sign-In & Security -> App-Specific Passwords.
- Gmail: myaccount.google.com -> Security -> App passwords.

## Develop
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
