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
