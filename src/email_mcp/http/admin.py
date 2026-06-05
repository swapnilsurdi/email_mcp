"""Operator CLI for the HTTP service (run on the host / inside the container).

Until the Matrix bot exists (Phase 2), this is how a login token is issued:

    python -m email_mcp.http.admin init
    python -m email_mcp.http.admin login-token @admin:chat.surdi.in [--ttl 86400]

The printed link opens the dashboard as that user.
"""
import argparse
import time

from email_mcp import runtime
from email_mcp.http import db


def main(argv=None):
    runtime.load_dotenv()
    ap = argparse.ArgumentParser(prog="email-mcp http admin")
    ap.add_argument("--db", default=None, help="state DB path (default: runtime)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the HTTP tables")
    lt = sub.add_parser("login-token", help="issue a 24h dashboard login token")
    lt.add_argument("matrix_user", help="e.g. @admin:chat.surdi.in")
    lt.add_argument("--ttl", type=int, default=86400)
    args = ap.parse_args(argv)

    db_path = args.db or runtime.db_path()
    db.init_http_tables(db_path)
    if args.cmd == "init":
        print(f"initialized HTTP tables in {db_path}")
        return
    if args.cmd == "login-token":
        import os
        now = time.time()
        uid = db.get_or_create_user(db_path, args.matrix_user, now)
        raw = db.issue_login_token(db_path, uid, now, ttl=args.ttl)
        base = os.environ.get("EMAIL_MCP_BASE_URL",
                              "https://email-mcp.surdi.in").rstrip("/")
        print(f"user:  {args.matrix_user} (id {uid})")
        print(f"token: {raw}")
        print(f"link:  {base}/?token={raw}")
        print(f"valid: {args.ttl}s — the link signs in ONCE (it swaps for a session "
              "cookie); unredeemed, the token also works as an API Bearer value")


if __name__ == "__main__":
    main()
