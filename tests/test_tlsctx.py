import ssl

from email_mcp import tlsctx


def test_ssl_context_verifies_and_checks_hostname():
    ctx = tlsctx.ssl_context()
    # Security posture: verification + hostname checking must never be disabled.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
