"""MCP tools for the HTTP surface: the same email operations as the stdio server, but
bound to the authenticated agent key's mailbox, gated by scopes, and usage-metered.

Layout: each `tool_*` is a PURE function taking (db_path, master_key, ...params) plus the
same injectable `fetch_fn`/`send_fn`/`connect_fn` hooks the core uses — unit-testable by
just binding `auth.current_principal`. `build_mcp()` wraps them in a FastMCP instance
(stateless streamable-http) for mounting; the principal arrives via MCPAuthMiddleware.

Differences from stdio: there is no `set_default_account` (an agent key is bound to ONE
mailbox); `list_accounts` reports that mailbox. The shared in-memory recent-cache is NOT
used here (mc=None) — its recent-index is keyed per folder, which would bleed between
mailboxes; per-mailbox keying is a planned optimisation.
"""
import time

from email_mcp import email_ops, runtime, security
from email_mcp.http import auth, db
from email_mcp.providers import imap_account


def _mailbox(db_path, master_key, principal):
    """(account, policy, None) or (None, None, structured-error)."""
    acc = db.mailbox_account(db_path, principal["mailbox_id"], master_key=master_key)
    if acc is None:
        return None, None, {
            "error": "mailbox_unavailable",
            "detail": "the mailbox bound to this key is not configured, was logged "
                      "out, or was deleted; the owner must (re)connect it on the "
                      "dashboard."}
    policy = security.policy_from_mapping(
        db.mailbox_policy_dict(db_path, principal["mailbox_id"]))
    return acc, policy, None


# ---- read tools -------------------------------------------------------------------

def tool_list_accounts(db_path, master_key):
    p, err = auth.check_scope("read")
    if err:
        return err
    mb = db.get_mailbox(db_path, p["mailbox_id"])
    if mb is None:
        return {"error": "mailbox_unavailable"}
    return [{"name": mb["name"], "email": mb["email"], "default": True,
             "connected": bool(mb["enc_password"]) and mb["status"] == "active"}]


def tool_list_folders(db_path, master_key, now=None,
                      folders_fn=imap_account.list_folders):
    p, err = auth.check_scope("read")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    db.bump_usage(db_path, p["token_id"], "read", now or time.time())
    return policy.filter_readable(folders_fn(acc))


def tool_get_emails(db_path, master_key, filters=None, query=None, folders=None,
                    include_sent=False, strip_to_text=False, page=1, page_size=20,
                    cached=False, body=True, from_address=None, subject=None,
                    since=None, has_attachment=False, fresh=False, now=None,
                    fetch_fn=imap_account.fetch_folder,
                    folders_fn=imap_account.list_folders):
    p, err = auth.check_scope("read")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    searched = bool(query or from_address or subject or since
                    or (filters and "criteria" in filters))
    db.bump_usage(db_path, p["token_id"], "search" if searched else "read",
                  now or time.time())
    return email_ops.get_emails(
        db_path, acc, filters=filters, query=query, folders=folders,
        include_sent=include_sent, strip_to_text=strip_to_text, page=page,
        page_size=page_size, cached=cached, body=body, from_=from_address,
        subject=subject, since=since, has_attachment=has_attachment, fresh=fresh,
        mc=None, policy=policy, fetch_fn=fetch_fn, folders_fn=folders_fn)


def tool_download_attachment(db_path, master_key, message_id, filename=None,
                             index=None, dest_dir=None, folders=None, overwrite=False,
                             download_all=False, return_base64=False, uid=None,
                             folder=None, now=None,
                             fetch_fn=imap_account.fetch_message,
                             folders_fn=imap_account.list_folders):
    p, err = auth.check_scope("read")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    db.bump_usage(db_path, p["token_id"], "read", now or time.time())
    search_folders = folders or folders_fn(acc)
    return email_ops.download_attachment(
        acc, message_id, folders=search_folders,
        dest_dir=dest_dir or runtime.download_dir(), filename=filename, index=index,
        overwrite=overwrite, download_all=download_all, return_base64=return_base64,
        uid=uid, folder=folder, policy=policy, fetch_fn=fetch_fn)


# ---- write tools (mark / move) ------------------------------------------------------

def _writable_folders(acc, policy, folders, folders_fn, message_id):
    requested = folders or folders_fn(acc)
    readable = policy.filter_readable(requested)
    if not readable:
        return None, {"error": "folders_blocked", "message_id": message_id,
                      "detail": "security policy blocks every requested folder"}
    return readable, None


def tool_mark_email(db_path, master_key, message_id, read, folders=None, now=None,
                    connect_fn=None, folders_fn=imap_account.list_folders):
    p, err = auth.check_scope("write")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    search_folders, err = _writable_folders(acc, policy, folders, folders_fn, message_id)
    if err:
        return err
    db.bump_usage(db_path, p["token_id"], "read", now or time.time())
    kwargs = {"connect_fn": connect_fn} if connect_fn else {}
    return imap_account.mark_message(acc, message_id, read=read,
                                     folders=search_folders, policy=policy, **kwargs)


def tool_move_email(db_path, master_key, message_id, dest_folder, folders=None,
                    now=None, connect_fn=None, folders_fn=imap_account.list_folders):
    p, err = auth.check_scope("write")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    search_folders, err = _writable_folders(acc, policy, folders, folders_fn, message_id)
    if err:
        return err
    db.bump_usage(db_path, p["token_id"], "read", now or time.time())
    kwargs = {"connect_fn": connect_fn} if connect_fn else {}
    return imap_account.move_message(acc, message_id, dest_folder=dest_folder,
                                     folders=search_folders, policy=policy, **kwargs)


# ---- send -------------------------------------------------------------------------

def tool_send_email(db_path, master_key, to, subject, body, tags=None,
                    attachments=None, allow_duplicate=False, idempotency_key=None,
                    now=None, send_fn=None):
    p, err = auth.check_scope("send")
    if err:
        return err
    acc, policy, err = _mailbox(db_path, master_key, p)
    if err:
        return err
    now = now or time.time()
    kwargs = {"send_fn": send_fn} if send_fn else {}
    result = email_ops.send_email(
        db_path, acc, to=to, subject=subject, body=body, tags=tags,
        attachments=attachments, allow_duplicate=allow_duplicate,
        idempotency_key=idempotency_key, policy=policy, now=now, **kwargs)
    status = (result or {}).get("status", "")
    if status == "sent":
        db.bump_usage(db_path, p["token_id"], "send", now)
    elif status.upper() == "BLOCKED" or "blocked" in status:
        db.bump_usage(db_path, p["token_id"], "blocked", now)
    return result


# ---- FastMCP wiring -----------------------------------------------------------------

INSTRUCTIONS = (
    "Multi-tenant IMAP/SMTP email over HTTP. Your agent key (Authorization: Bearer) is "
    "bound to one mailbox and a set of scopes — read, write, send. Reads never mark "
    "mail as read. Structured {error} results indicate missing scopes or policy blocks."
)


def build_mcp(db_path_fn, master_key_fn):
    """A FastMCP exposing the tool set; mount its streamable_http_app behind
    MCPAuthMiddleware. Stateless: every JSON-RPC call is one authenticated POST."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    # DNS-rebinding protection (Host-header allowlisting) is for UNAUTHENTICATED
    # localhost servers reachable from a hostile browser page. Every request here
    # must present a valid agent key (MCPAuthMiddleware), and the service sits behind
    # nginx on a private tailnet — so we disable it rather than maintain a Host
    # allowlist that breaks confusingly (HTTP 421) on proxy/hostname changes.
    mcp = FastMCP("email-mcp-http", instructions=INSTRUCTIONS,
                  stateless_http=True, streamable_http_path="/",
                  transport_security=TransportSecuritySettings(
                      enable_dns_rebinding_protection=False))

    @mcp.tool()
    def list_accounts() -> list:
        """The mailbox this agent key is bound to (and whether it is connected)."""
        return tool_list_accounts(db_path_fn(), master_key_fn())

    @mcp.tool()
    def list_folders() -> list:
        """List mail folders (folders the security policy blocks are omitted)."""
        return tool_list_folders(db_path_fn(), master_key_fn())

    @mcp.tool()
    def get_emails(filters: dict = None, query: str = None, folders: list = None,
                   include_sent: bool = False, strip_to_text: bool = False,
                   page: int = 1, page_size: int = 20, cached: bool = False,
                   body: bool = True, from_address: str = None, subject: str = None,
                   since: str = None, has_attachment: bool = False,
                   fresh: bool = False) -> dict:
        """Fetch or search emails. Never marks mail read. Defaults to INBOX + Junk.
        With search terms (`query`/`from_address`/`subject`/`since`) the search runs
        server-side over the whole mailbox; without them you get the recent window
        (`searched_window_only` in the result tells you which). `body=false` omits
        bodies. Each message includes `uid`/`uidvalidity` for follow-up actions."""
        return tool_get_emails(
            db_path_fn(), master_key_fn(), filters=filters, query=query,
            folders=folders, include_sent=include_sent, strip_to_text=strip_to_text,
            page=page, page_size=page_size, cached=cached, body=body,
            from_address=from_address, subject=subject, since=since,
            has_attachment=has_attachment, fresh=fresh)

    @mcp.tool()
    def download_attachment(message_id: str, filename: str = None, index: int = None,
                            folders: list = None, overwrite: bool = False,
                            download_all: bool = False, return_base64: bool = False,
                            uid: int = None, folder: str = None) -> dict:
        """Download a message's attachment(s) — read-only. `return_base64=true` returns
        bytes inline (≤256KB); otherwise files land in the server's download volume.
        Select by `filename`/`index`, or `download_all=true`."""
        return tool_download_attachment(
            db_path_fn(), master_key_fn(), message_id, filename=filename, index=index,
            folders=folders, overwrite=overwrite, download_all=download_all,
            return_base64=return_base64, uid=uid, folder=folder)

    @mcp.tool()
    def mark_email(message_id: str, read: bool, folders: list = None) -> dict:
        """Mark a message read/unread (requires the `write` scope; protected folders
        are refused)."""
        return tool_mark_email(db_path_fn(), master_key_fn(), message_id, read,
                               folders=folders)

    @mcp.tool()
    def move_email(message_id: str, dest_folder: str, folders: list = None) -> dict:
        """Move a message to another folder (requires the `write` scope; protected and
        blocked folders are refused — with the default policy mail cannot be deleted)."""
        return tool_move_email(db_path_fn(), master_key_fn(), message_id, dest_folder,
                               folders=folders)

    @mcp.tool()
    def send_email(to: list, subject: str, body: str, tags: dict = None,
                   attachments: list = None, allow_duplicate: bool = False,
                   idempotency_key: str = None) -> dict:
        """Send an email (requires the `send` scope). Idempotent: a duplicate send to
        the same recipients within 10 minutes is BLOCKED. Recipients must satisfy the
        mailbox's security policy or the send is BLOCKED with reason
        recipient_not_allowed."""
        return tool_send_email(db_path_fn(), master_key_fn(), to=to, subject=subject,
                               body=body, tags=tags, attachments=attachments,
                               allow_duplicate=allow_duplicate,
                               idempotency_key=idempotency_key)

    return mcp
