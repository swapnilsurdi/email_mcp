import base64
import email
import imaplib
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

from email_mcp import textutil, tlsctx


CONNECT_TIMEOUT = 30
_UID_RE = re.compile(rb"UID (\d+)")


def _default_connect(acc):
    # ssl_context verifies the cert chain + hostname; IMAP4_SSL's implicit default
    # does NOT verify, so we always pass our own.
    imap = imaplib.IMAP4_SSL(
        acc["imap_host"], acc.get("imap_port", 993),
        ssl_context=tlsctx.ssl_context(), timeout=CONNECT_TIMEOUT)
    imap.login(acc["email"], acc["password"])
    return imap


def _mutf7_encode(text):
    """Encode a mailbox name to IMAP modified UTF-7 (RFC 3501 §5.1.3). Printable
    ASCII passes through; '&' becomes '&-'; other chars are base64(UTF-16BE)
    between '&' and '-', with '/' replaced by ','."""
    out = []
    run = []

    def flush():
        if run:
            b64 = base64.b64encode("".join(run).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


def _mutf7_decode(text):
    """Decode an IMAP modified UTF-7 mailbox name back to a normal string."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            j = text.find("-", i)
            if j == -1:               # malformed; pass through verbatim
                out.append(text[i:])
                break
            chunk = text[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * ((4 - len(b64) % 4) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _encode_folder(folder):
    """Prepare a mailbox name for the wire: modified-UTF7 encode (for non-ASCII),
    then quote + escape. imaplib does NOT quote, so a name with a space (e.g.
    iCloud's "Deleted Messages") would otherwise become two tokens and the server
    rejects it with a BAD Parse Error."""
    encoded = _mutf7_encode(folder)
    escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _parse_folder_name(line):
    # line like: (\HasNoChildren) "/" "Job/Job Applications"
    s = line.decode() if isinstance(line, bytes) else line
    parts = s.split('"')
    if len(parts) >= 3:
        return _mutf7_decode(parts[-2])
    return None


def list_folders(acc, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        status, data = imap.list()
        if status != "OK":
            return []
        names = []
        for line in data:
            name = _parse_folder_name(line)
            if name:
                names.append(name)
        return names
    finally:
        _safe_logout(imap)


def _safe_logout(imap):
    try:
        imap.logout()
    except Exception:
        pass


def _decode_hdr(value):
    out = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return " ".join(out)


def _parse_date(date_str):
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_uid(prefix):
    """Pull the UID out of a FETCH response prefix like b'5 (UID 12345 BODY[] {2048}'."""
    m = _UID_RE.search(prefix or b"")
    return int(m.group(1)) if m else None


def _uidvalidity(imap):
    """The mailbox's UIDVALIDITY (set by SELECT/EXAMINE), or None. Together with a UID
    it forms a stable per-folder handle for messages with no/duplicate Message-ID."""
    try:
        vals = imap.untagged_responses.get("UIDVALIDITY")
        if vals:
            return int(vals[0])
    except Exception:
        pass
    return None


def _parse_fetch_items(fetched, acc, folder, uidvalidity):
    """Turn raw FETCH response items into our message dicts. `message_id` is the real
    header value or "" when absent (callers fall back to uid/folder for identity)."""
    out = []
    for item in fetched:
        if not isinstance(item, tuple) or len(item) < 2 or item[1] is None:
            continue
        msg = email.message_from_bytes(item[1])
        out.append({
            "message_id": msg.get("Message-ID", "") or "",
            "from_address": _decode_hdr(msg.get("From", "")),
            "to_address": _decode_hdr(msg.get("To", acc["email"])),
            "subject": _decode_hdr(msg.get("Subject", "")),
            "body": textutil.extract_text(msg),
            "attachments": textutil.list_attachments(msg),
            "received_date": _parse_date(msg.get("Date", "")).isoformat(),
            "folder": folder,
            "uid": _parse_uid(item[0]),
            "uidvalidity": uidvalidity,
        })
    return out


def fetch_folder(acc, folder, criteria, limit=None, connect_fn=_default_connect):
    """Fetch messages matching `criteria` from one folder. Never marks read.

    When `limit` is set, only the most recent `limit` matches (highest sequence
    numbers) are fetched. All matches are pulled in a SINGLE batched FETCH command
    rather than one round-trip per message (which timed out on large folders)."""
    imap = connect_fn(acc)
    try:
        status, _ = imap.select(_encode_folder(folder), readonly=True)
        if status != "OK":
            return []
        status, data = imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        nums = data[0].split()
        if limit is not None and len(nums) > limit:
            nums = nums[-limit:]            # most recent by arrival
        if not nums:
            return []
        msg_set = b",".join(nums).decode("ascii")
        status, fetched = imap.fetch(msg_set, "(UID BODY.PEEK[])")
        if status != "OK" or not fetched:
            return []
        return _parse_fetch_items(fetched, acc, folder, _uidvalidity(imap))
    finally:
        try:
            imap.close()
        except Exception:
            pass
        _safe_logout(imap)


def fetch_inbox_recent(acc, folder, count, since_uid=None, connect_fn=_default_connect):
    """For the prefetch poller: return (uidvalidity, max_uid, entries) for the latest
    `count` messages of `folder`. Read-only, BODY.PEEK[] (never marks read).

    When `since_uid` is given, only messages with a strictly greater UID are fetched
    (the cheap delta path — steady state with no new mail does a single UID SEARCH and
    no body FETCH). Note IMAP's `N:*` always matches at least the highest message, so we
    filter to uid > since_uid ourselves."""
    imap = connect_fn(acc)
    try:
        status, _ = imap.select(_encode_folder(folder), readonly=True)
        if status != "OK":
            return (None, None, [])
        uidvalidity = _uidvalidity(imap)
        if since_uid is not None:           # 0 is a valid UID floor, don't treat as None
            status, data = imap.uid("SEARCH", None, "UID", "%d:*" % (since_uid + 1))
            uids = [int(x) for x in (data[0].split() if data and data[0] else [])]
            uids = sorted(u for u in uids if u > since_uid)
            if not uids:
                return (uidvalidity, since_uid, [])     # nothing new
        else:
            status, data = imap.uid("SEARCH", None, "ALL")
            uids = sorted(int(x) for x in (data[0].split() if data and data[0] else []))
            if not uids:
                return (uidvalidity, None, [])
        uids = uids[-count:]
        uidset = ",".join(str(u) for u in uids)
        status, fetched = imap.uid("FETCH", uidset, "(UID BODY.PEEK[])")
        entries = _parse_fetch_items(fetched, acc, folder, uidvalidity) \
            if status == "OK" and fetched else []
        return (uidvalidity, max(uids), entries)
    finally:
        try:
            imap.close()
        except Exception:
            pass
        _safe_logout(imap)


def _find_uid(imap, message_id):
    status, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
    if status == "OK" and data and data[0]:
        return data[0].split()[0]
    return None


def _locate(imap, message_id, folders, readonly=False):
    for folder in folders:
        status, _ = imap.select(_encode_folder(folder), readonly=readonly)
        if status != "OK":
            continue
        uid = _find_uid(imap, message_id)
        if uid:
            return folder, uid
    return None, None


def fetch_message(acc, message_id, folders, uid=None, folder=None,
                  connect_fn=_default_connect):
    """Locate a message and return (folder, email.message.Message). Read-only and uses
    BODY.PEEK[] so it NEVER marks the message read. Identity: when `uid`+`folder` are
    given it fetches that message directly (the robust path for mail with an absent or
    duplicate Message-ID); otherwise it searches `folders` by Message-ID header. Returns
    (None, None) if not found."""
    imap = connect_fn(acc)
    try:
        if uid and folder:
            status, _ = imap.select(_encode_folder(folder), readonly=True)
            if status != "OK":
                return None, None
            loc_folder, loc_uid = folder, str(uid)
        else:
            loc_folder, loc_uid = _locate(imap, message_id, folders, readonly=True)
            if not loc_uid:
                return None, None
        folder = loc_folder
        status, data = imap.uid("FETCH", loc_uid, "(BODY.PEEK[])")
        if status != "OK" or not data:
            return None, None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and item[1] is not None:
                return folder, email.message_from_bytes(item[1])
        return None, None
    finally:
        try:
            imap.close()
        except Exception:
            pass
        _safe_logout(imap)


def mark_message(acc, message_id, read, folders, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        folder, uid = _locate(imap, message_id, folders)
        if not uid:
            return {"error": "not_found", "message_id": message_id}
        op = "+FLAGS" if read else "-FLAGS"
        imap.uid("STORE", uid, op, "(\\Seen)")
        return {"message_id": message_id, "read": read, "folder": folder}
    finally:
        _safe_logout(imap)


def move_message(acc, message_id, dest_folder, folders, connect_fn=_default_connect):
    imap = connect_fn(acc)
    try:
        folder, uid = _locate(imap, message_id, folders)
        if not uid:
            return {"error": "not_found", "message_id": message_id}
        dest = _encode_folder(dest_folder)
        status, _ = imap.uid("MOVE", uid, dest)
        if status != "OK":
            imap.uid("COPY", uid, dest)
            imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            imap.expunge()
        return {"message_id": message_id, "dest_folder": dest_folder, "from_folder": folder}
    finally:
        _safe_logout(imap)
