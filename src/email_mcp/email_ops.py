import time
from datetime import datetime, timedelta, timezone

from email_mcp import cache, ledger, textutil
from email_mcp.providers import imap_account, smtp_send

DEFAULT_WINDOW_DAYS = 90
MAX_TOKENS = 100_000


def _default_criteria(filters):
    """Pass filters through as IMAP criteria; default to SINCE 90 days."""
    if filters and "criteria" in filters:
        return list(filters["criteria"])
    since = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS))
    return ["SINCE", since.strftime("%d-%b-%Y")]


def _select_folders(all_folders, folders, include_sent):
    """Explicit `folders` overrides. Otherwise default to INBOX + Junk/Spam (fast;
    a full scan of every folder times out on large mailboxes). `include_sent` adds
    the Sent folder(s). Any other folder must be requested explicitly via `folders`."""
    if folders:
        return folders
    out = []
    for f in all_folders:
        low = f.lower()
        if low == "inbox" or "junk" in low or "spam" in low:
            out.append(f)
        elif include_sent and "sent" in low:
            out.append(f)
    return out


def _matches_query(msg, query):
    if not query:
        return True
    q = query.lower()
    return q in (msg.get("subject", "").lower()) or q in (msg.get("body", "").lower())


def _collect(acc, target, criteria, query, strip_to_text, fetch_fn, limit):
    merged = []
    for folder in target:
        for msg in fetch_fn(acc, folder, criteria, limit=limit):
            if not _matches_query(msg, query):
                continue
            if strip_to_text:
                msg = {**msg, "body": textutil.strip_non_ascii(msg.get("body", ""))}
            merged.append(msg)
    merged.sort(key=lambda m: m.get("received_date", ""), reverse=True)
    return merged


def get_emails(db_path, acc, filters, query, folders, include_sent,
               strip_to_text, page, page_size, cached=False,
               fetch_fn=imap_account.fetch_folder,
               folders_fn=imap_account.list_folders, now=None):
    """cached=False (default): bounded + fresh — fetch only the most-recent
    page*page_size per folder, never cached. cached=True: full fetch, cached 60 min
    and paginated from cache (use for stable paging over a fixed result set)."""
    target = _select_folders(folders_fn(acc), folders, include_sent)
    criteria = _default_criteria(filters)

    if cached:
        key = cache.make_key(
            acc["name"],
            {"filters": filters, "query": query, "folders": folders},
            include_sent, strip_to_text,
        )
        result = cache.get(db_path, key, now=now)
        from_cache = result is not None
        if result is None:
            result = _collect(acc, target, criteria, query, strip_to_text,
                              fetch_fn, limit=None)
            cache.set(db_path, key, result, now=now)
    else:
        from_cache = False
        result = _collect(acc, target, criteria, query, strip_to_text,
                          fetch_fn, limit=page * page_size)

    total = len(result)
    start = (page - 1) * page_size
    page_items = result[start:start + page_size]
    page_items, truncated = textutil.truncate_to_budget(page_items, MAX_TOKENS)
    return {
        "emails": page_items,
        "page": page,
        "page_size": page_size,
        "total_estimate": total,
        "truncated": truncated,
        "from_cache": from_cache,
    }


def send_email(db_path, acc, to, subject, body, tags,
               send_fn=smtp_send.send, now=None):
    now = time.time() if now is None else now
    block = ledger.check_block(db_path, to, subject, body, now=now)
    if block is not None:
        return block
    ids = ledger.record_queued(db_path, acc["name"], to, subject, body, tags, now=now)
    try:
        message_id = send_fn(acc, to, subject, body)
    except Exception as e:
        ledger.mark_failed(db_path, ids, now=now)
        return {"status": "failed", "error": str(e)}
    ledger.mark_sent(db_path, ids, message_id, now=now)
    return {"status": "sent", "message_id": message_id}
