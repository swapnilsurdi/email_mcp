from email_mcp import mcache, prefetch


def _entry(uid, mid=None, rd=None):
    return {"message_id": mid or f"<{uid}@x>", "uid": uid, "uidvalidity": 10,
            "folder": "INBOX", "body": "b", "subject": "s", "attachments": [],
            "received_date": rd or f"2026-06-02T10:00:{uid:02d}+00:00"}


ACC = {"name": "a", "email": "me@x.com"}


def test_full_then_delta_merges_newest_first():
    calls = []

    def fake_fetch(acc, folder, count, since_uid=None):
        calls.append(since_uid)
        if since_uid is None:                  # initial full pull
            return (10, 3, [_entry(3), _entry(2), _entry(1)])
        return (10, 5, [_entry(5), _entry(4)])  # delta: two new

    c = mcache.MessageCache(recent_ttl=999)
    state = {"uidvalidity": None, "max_uid": None, "entries": []}

    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=1.0, fetch_fn=fake_fetch)
    assert calls[0] is None                    # first cycle is a full pull
    assert state["max_uid"] == 3

    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=2.0, fetch_fn=fake_fetch)
    assert calls[1] == 3                        # second cycle deltas from max_uid
    assert state["max_uid"] == 5
    got = c.get_recent("INBOX", 5, now=2.0)
    assert [m["uid"] for m in got] == [5, 4, 3, 2, 1]   # merged, newest-first


def test_no_new_mail_restamps_freshness_without_fetch():
    def fake_fetch(acc, folder, count, since_uid=None):
        if since_uid is None:
            return (10, 2, [_entry(2), _entry(1)])
        return (10, 2, [])                      # nothing new
    c = mcache.MessageCache(recent_ttl=50)
    state = {"uidvalidity": None, "max_uid": None, "entries": []}
    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=1000.0, fetch_fn=fake_fetch)
    # second cycle, no new mail, much later but within a fresh re-stamp
    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=1040.0, fetch_fn=fake_fetch)
    got = c.get_recent("INBOX", 2, now=1041.0)   # fresh because re-stamped at 1040
    assert got is not None and [m["uid"] for m in got] == [2, 1]


def test_uidvalidity_change_resets_and_refetches():
    seq = []

    def fake_fetch(acc, folder, count, since_uid=None):
        seq.append(since_uid)
        if since_uid is None and not seq[:-1]:
            return (10, 9, [_entry(9)])          # first full pull, validity 10
        # delta call returns a NEW uidvalidity -> triggers reset+full
        if since_uid is not None:
            return (77, 1, [_entry(1)])          # validity changed under us
        return (77, 1, [_entry(1)])              # the forced full refetch
    c = mcache.MessageCache(recent_ttl=999)
    state = {"uidvalidity": None, "max_uid": None, "entries": []}
    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=1.0, fetch_fn=fake_fetch)
    prefetch.run_cycle(state, lambda: ACC, c, "INBOX", 50, now=2.0, fetch_fn=fake_fetch)
    assert state["uidvalidity"] == 77
    # entries were reset (not merged across the validity change)
    got = c.get_recent("INBOX", 1, now=2.0)
    assert [m["uid"] for m in got] == [1]


def test_start_returns_none_when_disabled():
    assert prefetch.start(lambda: ACC, mcache.MessageCache(), interval=0) is None
