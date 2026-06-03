from email_mcp import mcache


def _e(mid, body="x", folder="INBOX", uid=1, uidvalidity=10):
    return {"message_id": mid, "body": body, "folder": folder, "uid": uid,
            "uidvalidity": uidvalidity, "subject": "s", "attachments": []}


def test_cache_key_prefers_message_id():
    assert mcache.cache_key(_e("<a@x>")) == "<a@x>"


def test_cache_key_falls_back_to_uid_when_no_message_id():
    e = _e("", folder="INBOX", uid=42, uidvalidity=99)
    assert mcache.cache_key(e) == "uid:INBOX:99:42"


def test_upsert_and_get():
    c = mcache.MessageCache()
    c.upsert([_e("<a@x>"), _e("<b@x>")])
    assert c.get("<a@x>")["message_id"] == "<a@x>"
    assert c.get("<missing>") is None


def test_evicts_by_entry_count_lru():
    c = mcache.MessageCache(max_entries=2)
    c.upsert([_e("<a@x>")])
    c.upsert([_e("<b@x>")])
    c.get("<a@x>")                 # touch a -> b is now least-recent
    c.upsert([_e("<c@x>")])        # over cap -> evicts least-recent (b)
    assert c.get("<a@x>") is not None
    assert c.get("<b@x>") is None
    assert c.get("<c@x>") is not None
    assert c.stats()["entries"] == 2


def test_evicts_by_byte_budget():
    # each entry ~2012B (1500 body + 512 overhead): one fits in 2500, two don't.
    c = mcache.MessageCache(max_entries=100, max_bytes=2500, body_max=10 * 1024)
    c.upsert([_e("<a@x>", body="a" * 1500)])
    c.upsert([_e("<b@x>", body="b" * 1500)])
    assert c.stats()["entries"] == 1          # second push evicted the first
    assert c.stats()["bytes"] <= 2500


def test_body_trimmed_to_cap():
    c = mcache.MessageCache(body_max=100)
    c.upsert([_e("<a@x>", body="z" * 1000)])
    got = c.get("<a@x>")
    assert len(got["body"]) == 100
    assert got["body_truncated_in_cache"] is True


def test_recent_index_serves_then_expires():
    c = mcache.MessageCache(recent_ttl=50)
    entries = [_e("<a@x>", uid=3), _e("<b@x>", uid=2), _e("<c@x>", uid=1)]
    c.set_recent("INBOX", entries, now=1000.0)
    got = c.get_recent("INBOX", 2, now=1000.0 + 10)
    assert [m["message_id"] for m in got] == ["<a@x>", "<b@x>"]
    assert c.get_recent("INBOX", 2, now=1000.0 + 100) is None      # TTL expired


def test_get_recent_none_when_not_enough_or_evicted():
    c = mcache.MessageCache(max_entries=2, recent_ttl=999)
    c.set_recent("INBOX", [_e("<a@x>"), _e("<b@x>")], now=1.0)
    assert c.get_recent("INBOX", 5, now=2.0) is None    # asked for more than cached
    c.upsert([_e("<c@x>"), _e("<d@x>")])                # evicts a and b
    assert c.get_recent("INBOX", 2, now=3.0) is None    # index points at evicted


def test_invalidate_entry_and_folder():
    c = mcache.MessageCache()
    c.set_recent("INBOX", [_e("<a@x>")], now=1.0)
    c.invalidate(key="<a@x>", folder="INBOX")
    assert c.get("<a@x>") is None
    assert c.get_recent("INBOX", 1, now=1.0) is None
