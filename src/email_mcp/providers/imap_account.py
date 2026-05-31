import base64
import email
import imaplib
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

from email_mcp import textutil, tlsctx


CONNECT_TIMEOUT = 30


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


def fetch_folder(acc, folder, criteria, limit=None, connect_fn=_default_connect):
    """Fetch messages matching `criteria` from one folder. Never marks read.

    When `limit` is set, only the most recent `limit` matches (highest sequence
    numbers) are fetched. All matches are pulled in a SINGLE batched FETCH command
    rather than one round-trip per message (which timed out on large folders)."""
    imap = connect_fn(acc)
    out = []
    try:
        status, _ = imap.select(_encode_folder(folder), readonly=True)
        if status != "OK":
            return out
        status, data = imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return out
        nums = data[0].split()
        if limit is not None and len(nums) > limit:
            nums = nums[-limit:]            # most recent by arrival
        if not nums:
            return out
        msg_set = b",".join(nums).decode("ascii")
        status, fetched = imap.fetch(msg_set, "(BODY.PEEK[])")
        if status != "OK" or not fetched:
            return out
        for item in fetched:
            if not isinstance(item, tuple) or len(item) < 2 or item[1] is None:
                continue
            seq = item[0].split(b" ", 1)[0].decode("ascii", "replace")
            msg = email.message_from_bytes(item[1])
            out.append({
                "message_id": msg.get("Message-ID", f"{folder}-{seq}"),
                "from_address": _decode_hdr(msg.get("From", "")),
                "to_address": _decode_hdr(msg.get("To", acc["email"])),
                "subject": _decode_hdr(msg.get("Subject", "")),
                "body": textutil.extract_text(msg),
                "received_date": _parse_date(msg.get("Date", "")).isoformat(),
                "folder": folder,
            })
        return out
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


def _locate(imap, message_id, folders):
    for folder in folders:
        status, _ = imap.select(_encode_folder(folder), readonly=False)
        if status != "OK":
            continue
        uid = _find_uid(imap, message_id)
        if uid:
            return folder, uid
    return None, None


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
