import re

from bs4 import BeautifulSoup

CHARS_PER_TOKEN = 4


def extract_text(msg):
    """Best-effort plain text: prefer text/plain, fall back to HTML->text."""
    plain, html = None, None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain is None:
                plain = _decode(part)
            elif ctype == "text/html" and html is None:
                html = _decode(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _decode(msg)
        else:
            plain = _decode(msg)
    if plain:
        return plain
    if html:
        return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
    return ""


def _decode(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _iter_attachment_parts(msg):
    """Yield each part that represents a file attachment (or an inline file such as
    an embedded image). A part qualifies if its Content-Disposition is 'attachment'
    or it carries a filename. Multipart containers and the text body parts (which
    have no filename) are skipped, so this never yields the message body itself."""
    if not msg.is_multipart():
        disp = str(msg.get("Content-Disposition", "")).lower()
        if disp.startswith("attachment") and msg.get_filename():
            yield msg
        return
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = str(part.get("Content-Disposition", "")).lower()
        if disp.startswith("attachment") or part.get_filename():
            yield part


def list_attachments(msg):
    """Metadata only (never the bytes): one entry per attachment/inline file part,
    with a stable `index` usable to select it in download_attachment."""
    out = []
    for idx, part in enumerate(_iter_attachment_parts(msg)):
        payload = part.get_payload(decode=True) or b""
        disp = str(part.get("Content-Disposition", "")).lower()
        out.append({
            "index": idx,
            "filename": part.get_filename() or "",
            "mime_type": part.get_content_type(),
            "size": len(payload),
            "inline": disp.startswith("inline"),
        })
    return out


def extract_attachment(msg, filename=None, index=None):
    """Return (filename, mime_type, data_bytes) for the selected attachment, or None
    if there is no match. Selection precedence: explicit `index`, then exact
    `filename` match, then — only when the message has exactly one attachment — that
    one. Ambiguity (multiple attachments, no selector) returns None on purpose so the
    caller can prompt for a selector rather than guess."""
    parts = list(_iter_attachment_parts(msg))
    if not parts:
        return None
    chosen = None
    if index is not None:
        if 0 <= index < len(parts):
            chosen = parts[index]
    elif filename is not None:
        for p in parts:
            if (p.get_filename() or "") == filename:
                chosen = p
                break
    elif len(parts) == 1:
        chosen = parts[0]
    if chosen is None:
        return None
    data = chosen.get_payload(decode=True) or b""
    return (chosen.get_filename() or "", chosen.get_content_type(), data)


_UNSAFE_FN = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_filename(name, fallback="attachment"):
    """Reduce an UNTRUSTED attachment filename to a single safe path component.
    Email-supplied names are attacker-controlled, so we: take the basename for BOTH
    separators (a name crafted on one OS may use the other; os.path.basename only
    handles the host's), replace control/reserved characters, and reject names that
    resolve to traversal ('.', '..') or empty. Returns `fallback` if nothing usable
    remains. The caller still validates the final path is inside the download dir."""
    if not name:
        return fallback
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FN.sub("_", name).strip().strip(". ")
    if not name or name in (".", ".."):
        return fallback
    return name[:255]


def strip_non_ascii(text):
    """Drop non-ASCII characters (design spec §4.3: 'non-ASCII stripped')."""
    return "".join(c for c in text if ord(c) < 128)


def estimate_tokens(text):
    return len(text) // CHARS_PER_TOKEN


def truncate_to_budget(emails, max_tokens):
    """Trim email bodies so the total stays within max_tokens. Preserve count."""
    total = sum(estimate_tokens(e.get("body", "")) for e in emails)
    if total <= max_tokens:
        return emails, False
    n = max(len(emails), 1)
    per = max(max_tokens // n, 1)
    out = []
    for e in emails:
        b = e.get("body", "")
        if estimate_tokens(b) > per:
            b = b[: per * CHARS_PER_TOKEN]
        out.append({**e, "body": b})
    return out, True
