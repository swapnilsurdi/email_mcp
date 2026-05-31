import ssl

try:
    import certifi
    _CAFILE = certifi.where()
except Exception:  # pragma: no cover - certifi is a declared dependency
    _CAFILE = None


def ssl_context():
    """A TLS context that VERIFIES certificates (hostname + chain). Uses certifi's
    CA bundle when available — python.org macOS builds ship no usable system CA
    store, so the stdlib default verify paths are empty and verification fails.
    Verification and hostname checking are never disabled (design §8)."""
    ctx = ssl.create_default_context(cafile=_CAFILE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
