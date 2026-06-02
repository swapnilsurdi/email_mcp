import email.message
from email_mcp import textutil


def _msg(plain=None, html=None):
    if plain and html:
        m = email.message.EmailMessage()
        m.set_content(plain)
        m.add_alternative(html, subtype="html")
        return email.message_from_bytes(bytes(m))
    m = email.message.EmailMessage()
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(plain or "")
    return email.message_from_bytes(bytes(m))


def test_prefers_plain_text():
    assert "hello plain" in textutil.extract_text(_msg(plain="hello plain", html="<p>hi html</p>"))


def test_html_fallback_strips_tags():
    out = textutil.extract_text(_msg(html="<p>Click <a href='x'>here</a></p>"))
    assert "Click" in out and "here" in out and "<" not in out


def test_strip_non_ascii():
    assert textutil.strip_non_ascii("café — déjà ✓ vu") == "caf  dj  vu"


def test_estimate_tokens():
    assert textutil.estimate_tokens("a" * 400) == 100  # ~4 chars/token


def test_truncate_to_token_budget_trims_bodies_not_count():
    emails = [{"body": "x" * 4000} for _ in range(5)]  # ~1000 tokens each
    out, truncated = textutil.truncate_to_budget(emails, max_tokens=1000)
    assert len(out) == 5            # count preserved
    assert truncated is True
    assert sum(textutil.estimate_tokens(e["body"]) for e in out) <= 1000


# ---- attachments --------------------------------------------------------------

def _msg_with_attachments(*atts):
    """Build a multipart message: a text body plus each (filename, mime, data)."""
    m = email.message.EmailMessage()
    m.set_content("body text")
    for filename, mime, data in atts:
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return email.message_from_bytes(bytes(m))


def test_list_attachments_metadata_no_bytes():
    msg = _msg_with_attachments(
        ("report.pdf", "application/pdf", b"%PDF-1.4 fake"),
        ("data.csv", "text/csv", b"a,b,c\n1,2,3\n"))
    atts = textutil.list_attachments(msg)
    assert [a["filename"] for a in atts] == ["report.pdf", "data.csv"]
    assert atts[0]["mime_type"] == "application/pdf"
    assert atts[0]["size"] == len(b"%PDF-1.4 fake")
    assert atts[0]["index"] == 0 and atts[1]["index"] == 1
    # body text must NOT be listed as an attachment
    assert all("body" not in (a["filename"] or "") for a in atts)


def test_list_attachments_empty_for_plain_message():
    m = email.message.EmailMessage()
    m.set_content("just text")
    assert textutil.list_attachments(email.message_from_bytes(bytes(m))) == []


def test_extract_attachment_by_index_and_filename():
    msg = _msg_with_attachments(
        ("a.txt", "text/plain", b"alpha"),
        ("b.txt", "text/plain", b"bravo"))
    assert textutil.extract_attachment(msg, index=1) == ("b.txt", "text/plain", b"bravo")
    assert textutil.extract_attachment(msg, filename="a.txt")[2] == b"alpha"


def test_extract_attachment_single_needs_no_selector():
    msg = _msg_with_attachments(("only.bin", "application/octet-stream", b"\x00\x01"))
    name, mime, data = textutil.extract_attachment(msg)
    assert name == "only.bin" and data == b"\x00\x01"


def test_extract_attachment_ambiguous_returns_none():
    msg = _msg_with_attachments(
        ("a.txt", "text/plain", b"x"), ("b.txt", "text/plain", b"y"))
    assert textutil.extract_attachment(msg) is None        # 2 attachments, no selector
    assert textutil.extract_attachment(msg, index=9) is None  # out of range
    assert textutil.extract_attachment(msg, filename="nope.txt") is None


def test_safe_filename_strips_traversal_and_separators():
    assert textutil.safe_filename("../../etc/passwd") == "passwd"
    assert textutil.safe_filename("..\\..\\windows\\system32\\evil.dll") == "evil.dll"
    assert textutil.safe_filename("/abs/path/x.pdf") == "x.pdf"
    assert textutil.safe_filename("a/b/c.txt") == "c.txt"


def test_safe_filename_rejects_dotted_and_empty():
    assert textutil.safe_filename("..") == "attachment"
    assert textutil.safe_filename("") == "attachment"
    assert textutil.safe_filename("...") == "attachment"
    assert textutil.safe_filename("/") == "attachment"
    # control-only names sanitize to a harmless confined component (no separators)
    out = textutil.safe_filename("\x00\x01")
    assert "/" not in out and "\\" not in out and ".." not in out


def test_safe_filename_preserves_normal_names():
    assert textutil.safe_filename("Quarterly Report 2026.pdf") == "Quarterly Report 2026.pdf"
    assert textutil.safe_filename("archive.tar.gz") == "archive.tar.gz"
