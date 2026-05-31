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
