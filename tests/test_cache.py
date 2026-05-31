from email_mcp import cache


def test_make_key_is_stable_and_order_independent():
    k1 = cache.make_key("acct", {"b": 2, "a": 1}, include_sent=False, strip=False)
    k2 = cache.make_key("acct", {"a": 1, "b": 2}, include_sent=False, strip=False)
    assert k1 == k2


def test_set_then_get_within_ttl(db_path):
    cache.set(db_path, "k1", {"emails": [1, 2, 3]}, now=1000.0)
    assert cache.get(db_path, "k1", now=1000.0 + 59 * 60) == {"emails": [1, 2, 3]}


def test_get_returns_none_after_ttl(db_path):
    cache.set(db_path, "k1", {"x": 1}, now=1000.0)
    assert cache.get(db_path, "k1", now=1000.0 + 61 * 60) is None


def test_get_missing_returns_none(db_path):
    assert cache.get(db_path, "nope", now=1000.0) is None
