import os
import time
from datetime import datetime, timedelta, timezone

from email_mcp import cache, ledger, textutil
from email_mcp.providers import imap_account, smtp_send

DEFAULT_WINDOW_DAYS = 90
MAX_TOKENS = 100_000
# Cap a single attachment download (whole payload is held in memory before write).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


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


def send_email(db_path, acc, to, subject, body, tags, attachments=None,
               send_fn=smtp_send.send, now=None):
    now = time.time() if now is None else now
    block = ledger.check_block(db_path, to, subject, body, now=now)
    if block is not None:
        return block
    ids = ledger.record_queued(db_path, acc["name"], to, subject, body, tags, now=now)
    try:
        message_id = send_fn(acc, to, subject, body, attachments=attachments)
    except Exception as e:
        ledger.mark_failed(db_path, ids, now=now)
        return {"status": "failed", "error": str(e)}
    ledger.mark_sent(db_path, ids, message_id, now=now)
    return {"status": "sent", "message_id": message_id}


def _dedupe_path(path):
    """Avoid clobbering an existing file: 'name.pdf' -> 'name (1).pdf' -> ..."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{root} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _safe_dest(dest_dir, raw_name, overwrite):
    """Resolve a write path for an UNTRUSTED attachment filename, confined to a
    single file directly inside `dest_dir`. `raw_name` comes from the email, so it
    is sanitized to a bare component and the realpath is verified to sit directly
    under the realpath of `dest_dir` — a defense-in-depth check that defeats any
    traversal ('../', absolute paths, symlink tricks) that slipped past sanitizing."""
    base = os.path.realpath(dest_dir)
    name = textutil.safe_filename(raw_name)
    candidate = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(candidate) != base:
        raise ValueError("refused: attachment path escapes the download directory")
    return candidate if overwrite else _dedupe_path(candidate)


def download_attachment(acc, message_id, folders, dest_dir, filename=None,
                        index=None, overwrite=False,
                        fetch_fn=imap_account.fetch_message):
    """Locate a message by Message-ID (read-only; never marks read), extract the
    selected attachment, and write it into the sandboxed `dest_dir`. Returns the
    saved path + metadata, or a structured {error} (not raised) on any miss."""
    os.makedirs(dest_dir, exist_ok=True)
    found_folder, msg = fetch_fn(acc, message_id, folders)
    if msg is None:
        return {"error": "not_found", "message_id": message_id}
    # Decode only the selected attachment (not every part). list_attachments — which
    # decodes all of them for sizing — is consulted only on the miss path below.
    extracted = textutil.extract_attachment(msg, filename=filename, index=index)
    if extracted is None:
        available = textutil.list_attachments(msg)
        if not available:
            return {"error": "no_attachments", "message_id": message_id}
        return {"error": "attachment_not_selected", "message_id": message_id,
                "available": available,
                "detail": "Specify which attachment via `filename` or `index`."}
    att_name, mime_type, data = extracted
    if len(data) > MAX_DOWNLOAD_BYTES:
        return {"error": "attachment_too_large", "message_id": message_id,
                "filename": att_name, "size": len(data), "limit": MAX_DOWNLOAD_BYTES}
    dest_path = _safe_dest(dest_dir, att_name, overwrite)
    with open(dest_path, "wb") as f:
        f.write(data)
    return {
        "status": "downloaded",
        "message_id": message_id,
        "folder": found_folder,
        "filename": os.path.basename(dest_path),
        "original_filename": att_name,
        "mime_type": mime_type,
        "size": len(data),
        "saved_path": dest_path,
    }
