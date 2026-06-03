import os
import time
from datetime import datetime, timedelta, timezone

from email_mcp import cache, ledger, mcache as _mcache, textutil
from email_mcp.providers import imap_account, smtp_send

DEFAULT_WINDOW_DAYS = 90
MAX_TOKENS = 100_000
# Cap a single attachment download (whole payload is held in memory before write).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def _imap_date(s):
    """Accept an ISO-ish date and return IMAP's DD-Mon-YYYY; pass through if already so."""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%b-%Y")
        except (ValueError, TypeError):
            pass
    return s


def _build_criteria(filters, query, from_, subject, since):
    """Resolve IMAP search criteria + whether this is a real server-side search.

    Precedence: an explicit `filters.criteria` is used verbatim (back-compat) and any
    `query` then applies client-side. Otherwise we compile structured params into a
    server-side search — `query` becomes a full-mailbox IMAP TEXT search (so a hit
    anywhere in the mailbox is found, not just the recent window). With no search terms
    at all we fall back to the bounded SINCE-90-days default."""
    if filters and "criteria" in filters:
        return list(filters["criteria"]), True, query
    parts = []
    if from_:
        parts += ["FROM", from_]
    if subject:
        parts += ["SUBJECT", subject]
    if query:
        parts += ["TEXT", query]
    if since:
        parts += ["SINCE", _imap_date(since)]
    if parts:
        return parts, True, None            # query satisfied server-side
    default_since = datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS)
    return ["SINCE", default_since.strftime("%d-%b-%Y")], False, None


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


def _passes(msg, client_query, has_attachment):
    if client_query:
        q = client_query.lower()
        if q not in msg.get("subject", "").lower() and q not in msg.get("body", "").lower():
            return False
    if has_attachment and not msg.get("attachments"):
        return False
    return True


def _dedupe_entries(entries):
    """Drop duplicate messages (cross-folder, or a reused Message-ID) keeping the first."""
    seen, out = set(), []
    for e in entries:
        k = _mcache.cache_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _collect(acc, target, criteria, client_query, has_attachment, fetch_fn, limit,
             mc, use_cache, now):
    """Gather messages across `target` folders, serving the latest-window default from
    the in-memory cache when possible (use_cache) and writing fetched results back.
    Returns (filtered+deduped+sorted list, did_live_fetch)."""
    merged, did_live = [], False
    for folder in target:
        rows = None
        if use_cache and mc is not None:
            rows = mc.get_recent(folder, limit, now)
        if rows is None:
            rows = fetch_fn(acc, folder, criteria, limit=limit)
            did_live = True
            if mc is not None:
                # The default latest-window fetch IS the authoritative recent view;
                # a server-side search only seeds the content LRU (not the index).
                if use_cache:
                    mc.set_recent(folder, rows, now)
                else:
                    mc.upsert(rows)
        merged.extend(r for r in rows if _passes(r, client_query, has_attachment))
    merged = _dedupe_entries(merged)
    merged.sort(key=lambda m: m.get("received_date", ""), reverse=True)
    return merged, did_live


_INTERNAL_KEYS = ("body_truncated_in_cache",)


def _render(msg, strip_to_text, body):
    """Return a COPY (never an alias into the cache — a shared MessageCache entry must
    not escape to a caller that might mutate it) with internal plumbing keys dropped."""
    drop = set(_INTERNAL_KEYS)
    if not body:
        drop.add("body")
    out = {k: v for k, v in msg.items() if k not in drop}
    if body and strip_to_text:
        out["body"] = textutil.strip_non_ascii(out.get("body", ""))
    return out


def get_emails(db_path, acc, filters, query, folders, include_sent,
               strip_to_text, page, page_size, cached=False,
               body=True, from_=None, subject=None, since=None,
               has_attachment=False, fresh=False, mc=None,
               fetch_fn=imap_account.fetch_folder,
               folders_fn=imap_account.list_folders, now=None):
    """Fetch or search. Never marks read.

    Default (cached=False, no search terms): bounded + fast — serves the most-recent
    page*page_size per folder, from the in-memory cache when warm, else a fresh fetch.
    Search (query/from_/subject/since, or an explicit filters.criteria): runs SERVER-SIDE
    over the whole mailbox, so a match outside the recent window is still found.
    `searched_window_only` in the result tells you which mode ran — when True an empty
    result means "not in the recent window", NOT "doesn't exist anywhere".
    cached=True keeps the 60-min SQLite result-set cache for stable pagination.
    body=False omits message bodies (cheap headers+attachment metadata for searching).
    fresh=True bypasses the in-memory cache for a guaranteed-live read."""
    now = time.time() if now is None else now
    # Only list folders over IMAP when the caller didn't name them — otherwise an
    # explicit folders=[...] (e.g. the warm-cache INBOX read) pays no round-trip.
    target = folders if folders else _select_folders(
        folders_fn(acc), None, include_sent)
    criteria, did_server_search, client_query = _build_criteria(
        filters, query, from_, subject, since)
    searched_window_only = not (did_server_search or cached)

    if cached:
        key = cache.make_key(
            acc["name"],
            {"filters": filters, "query": query, "folders": folders,
             "from": from_, "subject": subject, "since": since,
             "has_attachment": has_attachment},
            include_sent, strip_to_text,
        )
        result = cache.get(db_path, key, now=now)
        from_cache = result is not None
        if result is None:
            result, _ = _collect(acc, target, criteria, client_query, has_attachment,
                                 fetch_fn, None, None, False, now)
            cache.set(db_path, key, result, now=now)
    else:
        use_cache = (not did_server_search) and (not fresh)
        result, did_live = _collect(acc, target, criteria, client_query, has_attachment,
                                    fetch_fn, page * page_size, mc, use_cache, now)
        from_cache = use_cache and not did_live

    total = len(result)
    start = (page - 1) * page_size
    page_items = [_render(m, strip_to_text, body) for m in result[start:start + page_size]]
    if body:
        page_items, truncated = textutil.truncate_to_budget(page_items, MAX_TOKENS)
    else:
        truncated = False
    return {
        "emails": page_items,
        "page": page,
        "page_size": page_size,
        "total_estimate": total,
        "truncated": truncated,
        "from_cache": from_cache,
        "searched_window_only": searched_window_only,
    }


def send_email(db_path, acc, to, subject, body, tags, attachments=None,
               allow_duplicate=False, idempotency_key=None,
               send_fn=smtp_send.send, now=None):
    """Idempotent send. By default a second mail to the same recipients within 10 min
    is BLOCKED (the runaway guard). `allow_duplicate=True` relaxes that to block only a
    true repeat (same recipients AND same subject/body), so distinct messages to the
    same person go through. `idempotency_key` overrides entirely: the send blocks iff
    that exact key was used inside the window — caller-controlled dedup."""
    now = time.time() if now is None else now
    # Resolve which dedup keys this send uses, and check+record the SAME set so a send
    # in one mode never arms a key that surprises a send in another mode.
    if idempotency_key:
        kinds = ("idempotency",)
    elif allow_duplicate:
        kinds = ("recipient_subject", "recipient_body")
    else:
        kinds = ledger.KEY_KINDS
    block = ledger.check_block(db_path, to, subject, body, now=now, kinds=kinds,
                               idempotency_key=idempotency_key)
    if block is not None:
        return block
    ids = ledger.record_queued(db_path, acc["name"], to, subject, body, tags, now=now,
                               kinds=kinds, idempotency_key=idempotency_key)
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


# Inline (base64) return is capped well below the disk cap — it lands in the model's
# context, so only small files (a CSV/ICS/key) make sense to return without a disk hop.
MAX_INLINE_BYTES = 256 * 1024


def _emit_attachment(att_name, mime_type, data, dest_dir, overwrite, return_base64,
                     message_id, folder):
    """Write one attachment to disk, or (return_base64) return its bytes inline."""
    if len(data) > MAX_DOWNLOAD_BYTES:
        return {"error": "attachment_too_large", "message_id": message_id,
                "filename": att_name, "size": len(data), "limit": MAX_DOWNLOAD_BYTES}
    base = {"message_id": message_id, "folder": folder,
            "filename": textutil.safe_filename(att_name),
            "original_filename": att_name, "mime_type": mime_type, "size": len(data)}
    if return_base64:
        if len(data) > MAX_INLINE_BYTES:
            return {"error": "attachment_too_large_for_inline", "size": len(data),
                    "limit": MAX_INLINE_BYTES, **base}
        import base64
        return {"status": "inline", "content_base64": base64.b64encode(data).decode(),
                **base}
    dest_path = _safe_dest(dest_dir, att_name, overwrite)
    with open(dest_path, "wb") as f:
        f.write(data)
    return {"status": "downloaded", "saved_path": dest_path,
            **{**base, "filename": os.path.basename(dest_path)}}


def download_attachment(acc, message_id, folders, dest_dir, filename=None,
                        index=None, overwrite=False, download_all=False,
                        return_base64=False, uid=None, folder=None,
                        fetch_fn=imap_account.fetch_message):
    """Fetch a message (read-only; never marks read) and save/return its attachment(s).
    Select one by `filename` or `index`; `download_all=True` takes every attachment; a
    lone attachment needs no selector. `return_base64=True` returns bytes inline (small
    files only) instead of writing to disk. `uid`+`folder` locate the message directly
    (robust for absent/duplicate Message-IDs). Misses return a structured {error}."""
    if not return_base64:
        os.makedirs(dest_dir, exist_ok=True)
    found_folder, msg = fetch_fn(acc, message_id, folders, uid=uid, folder=folder)
    if msg is None:
        return {"error": "not_found", "message_id": message_id}
    available = textutil.list_attachments(msg)
    if not available:
        return {"error": "no_attachments", "message_id": message_id}

    if download_all:
        results = []
        for a in available:
            ex = textutil.extract_attachment(msg, index=a["index"])
            if ex is not None:
                results.append(_emit_attachment(*ex, dest_dir, overwrite,
                                                return_base64, message_id, found_folder))
        ok = sum(1 for r in results if r.get("status") in ("downloaded", "inline"))
        return {"status": "downloaded_all", "message_id": message_id,
                "folder": found_folder, "count": ok, "attachments": results}

    extracted = textutil.extract_attachment(msg, filename=filename, index=index)
    if extracted is None:
        return {"error": "attachment_not_selected", "message_id": message_id,
                "available": available,
                "detail": "Specify `filename`/`index`, or set download_all=true."}
    return _emit_attachment(*extracted, dest_dir, overwrite, return_base64,
                            message_id, found_folder)
