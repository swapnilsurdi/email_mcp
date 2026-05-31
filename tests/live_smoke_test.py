"""Manual live test. NOT run in CI. Requires a real account in accounts.yml and
its password in Keychain. Run:  EMAIL_MCP_LIVE=1 .venv/bin/python tests/live_smoke_test.py
Reads only (never sends) so it is safe to run repeatedly.

Bounded to a narrow recent window (default 2 days) so it returns quickly instead
of pulling the full 90-day INBOX. Override with EMAIL_MCP_SMOKE_DAYS."""
import os
import sys
from datetime import datetime, timedelta, timezone

from email_mcp import email_ops, runtime


def main():
    if os.environ.get("EMAIL_MCP_LIVE") != "1":
        print("Set EMAIL_MCP_LIVE=1 to run.")
        sys.exit(0)
    days = int(os.environ.get("EMAIL_MCP_SMOKE_DAYS", "2"))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    acc = runtime.effective_account()
    print(f"Account: {acc['name']} {acc['email']}  (window: SINCE {since})")
    res = email_ops.get_emails(
        runtime.db_path(), acc, filters={"criteria": ["SINCE", since]},
        query=None, folders=["INBOX"], include_sent=False, strip_to_text=True,
        page=1, page_size=3)
    print("total_estimate:", res["total_estimate"], "from_cache:", res["from_cache"])
    for e in res["emails"]:
        print("-", e["subject"][:60], "|", e["from_address"][:40])


if __name__ == "__main__":
    main()
