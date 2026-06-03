from email_mcp import email_ops


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "imap_host": "h", "imap_port": 993}


def _emails(n, body="word match here", subj="S"):
    return [{"message_id": f"<{i}>", "from_address": "s@x.com",
             "to_address": "me@x.com", "subject": f"{subj}{i}",
             "body": body, "received_date": "2026-05-28T10:00:00+00:00",
             "folder": "INBOX"} for i in range(n)]


def test_get_emails_paginates_and_caches(db_path):
    calls = {"n": 0}

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        calls["n"] += 1
        return _emails(25) if folder == "INBOX" else []

    def fake_folders(acc, connect_fn=None):
        return ["INBOX", "Sent Messages"]

    r1 = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        cached=True, fetch_fn=fake_fetch, folders_fn=fake_folders, now=1000.0)
    assert len(r1["emails"]) == 20
    assert r1["total_estimate"] == 25
    assert r1["from_cache"] is False

    r2 = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=2, page_size=20,
        cached=True, fetch_fn=fake_fetch, folders_fn=fake_folders, now=1000.0 + 60)
    assert len(r2["emails"]) == 5
    assert r2["from_cache"] is True  # page 2 served from cache, no refetch


def test_query_searches_server_side(db_path):
    seen = {}

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen["criteria"] = criteria
        # a real server honors TEXT; emulate by returning only the match
        return [{"subject": "has needle", "body": "x", "from_address": "a",
                 "to_address": "b", "message_id": "<1>", "attachments": [],
                 "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"}]
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query="needle", folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=lambda a, connect_fn=None: ["INBOX"], now=1000.0)
    assert "TEXT" in seen["criteria"] and "needle" in seen["criteria"]
    assert out["searched_window_only"] is False        # a real search ran
    assert out["total_estimate"] == 1
    assert out["emails"][0]["message_id"] == "<1>"


def test_explicit_criteria_keeps_query_client_side(db_path):
    # Back-compat: when the caller passes raw filters.criteria AND a query, the query
    # is still applied client-side over the returned set.
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return [
            {"subject": "has needle", "body": "x", "message_id": "<1>", "attachments": [],
             "from_address": "a", "to_address": "b",
             "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
            {"subject": "no match", "body": "y", "message_id": "<2>", "attachments": [],
             "from_address": "a", "to_address": "b",
             "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
        ]
    out = email_ops.get_emails(
        db_path, ACC, filters={"criteria": ["ALL"]}, query="needle", folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=lambda a, connect_fn=None: ["INBOX"], now=1000.0)
    assert out["total_estimate"] == 1
    assert out["emails"][0]["message_id"] == "<1>"
    assert out["searched_window_only"] is False


def test_excludes_sent_by_default(db_path):
    seen = []

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen.append(folder)
        return []
    email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch,
        folders_fn=lambda a, connect_fn=None: ["INBOX", "Sent Messages", "Drafts"],
        now=1000.0)
    assert "Sent Messages" not in seen
    assert "Drafts" not in seen
    assert "INBOX" in seen


from email_mcp import ledger


def test_send_blocks_duplicate(db_path):
    sent = []

    def fake_send(acc, to, subject, body, attachments=None):
        sent.append((to, subject, body))
        return "<mid-1@x>"

    r1 = email_ops.send_email(
        db_path, ACC, to=["x@y.com"], subject="S", body="B",
        tags={"c": 1}, send_fn=fake_send, now=1000.0)
    assert r1["status"] == "sent"
    assert r1["message_id"] == "<mid-1@x>"

    r2 = email_ops.send_email(
        db_path, ACC, to=["x@y.com"], subject="totally different", body="other",
        tags=None, send_fn=fake_send, now=1000.0 + 30)
    assert r2["status"] == "BLOCKED"
    assert "recipient" in r2["matched"]
    assert r2["prior"]["tags"] == {"c": 1}
    assert len(sent) == 1  # second send never hit SMTP


def test_send_failure_records_failed_and_blocks(db_path):
    def boom(acc, to, subject, body, attachments=None):
        raise RuntimeError("smtp down")

    r1 = email_ops.send_email(
        db_path, ACC, to=["z@y.com"], subject="S", body="B",
        tags=None, send_fn=boom, now=2000.0)
    assert r1["status"] == "failed"

    r2 = email_ops.send_email(
        db_path, ACC, to=["z@y.com"], subject="S", body="B",
        tags=None, send_fn=lambda *a, **k: "<x>", now=2000.0 + 30)
    assert r2["status"] == "BLOCKED"
    assert r2["prior"]["status"] == "failed"


def test_default_folders_are_inbox_and_junk(db_path):
    seen = []

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen.append(folder)
        return []
    email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch,
        folders_fn=lambda a, connect_fn=None: [
            "INBOX", "Junk", "Archive", "Sent Messages", "Drafts", "Job/X"],
        now=1000.0)
    assert set(seen) == {"INBOX", "Junk"}


def test_include_sent_adds_sent_to_default(db_path):
    seen = []

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen.append(folder)
        return []
    email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=None,
        include_sent=True, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch,
        folders_fn=lambda a, connect_fn=None: [
            "INBOX", "Junk", "Sent Messages", "Archive"],
        now=1000.0)
    assert "Sent Messages" in seen and "INBOX" in seen and "Junk" in seen
    assert "Archive" not in seen


# ---- new: window flag, body mode, structured search, has_attachment, dedup -------

from email_mcp import mcache


def _only(folders):
    return lambda a, connect_fn=None: folders


def test_default_read_is_window_only(db_path):
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return _emails(2)
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=_only(["INBOX"]), now=1000.0)
    assert out["searched_window_only"] is True


def test_structured_params_build_server_criteria(db_path):
    seen = {}

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen["c"] = criteria
        return _emails(1)
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        from_="x@y.com", subject="hi", since="2026-01-01",
        fetch_fn=fake_fetch, folders_fn=_only(["INBOX"]), now=1000.0)
    assert seen["c"][:2] == ["FROM", "x@y.com"]
    assert "SUBJECT" in seen["c"] and "hi" in seen["c"]
    assert "SINCE" in seen["c"] and "01-Jan-2026" in seen["c"]
    assert out["searched_window_only"] is False


def test_body_false_omits_bodies(db_path):
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return _emails(2)
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20, body=False,
        fetch_fn=fake_fetch, folders_fn=_only(["INBOX"]), now=1000.0)
    assert out["emails"] and all("body" not in e for e in out["emails"])
    assert out["truncated"] is False


def test_has_attachment_filters(db_path):
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return [
            {"message_id": "<1>", "attachments": [{"index": 0, "filename": "a.pdf"}],
             "body": "", "subject": "s", "from_address": "a", "to_address": "b",
             "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
            {"message_id": "<2>", "attachments": [], "body": "", "subject": "s",
             "from_address": "a", "to_address": "b",
             "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
        ]
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        has_attachment=True, fetch_fn=fake_fetch, folders_fn=_only(["INBOX"]), now=1000.0)
    assert [m["message_id"] for m in out["emails"]] == ["<1>"]


def test_dedupes_same_message_across_folders(db_path):
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return [{"message_id": "<dup>", "attachments": [], "body": "b", "subject": "s",
                 "from_address": "a", "to_address": "b",
                 "received_date": "2026-05-28T10:00:00+00:00", "folder": folder}]
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX", "Junk"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=_only(["INBOX", "Junk"]), now=1000.0)
    assert out["total_estimate"] == 1


def test_in_memory_cache_serves_latest_without_fetch(db_path):
    mc = mcache.MessageCache(recent_ttl=999)
    mc.set_recent("INBOX", _emails(5), now=1000.0)

    def boom(*a, **k):
        raise AssertionError("must not fetch — cache is warm")
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=5,
        mc=mc, fetch_fn=boom, folders_fn=_only(["INBOX"]), now=1000.0)
    assert out["from_cache"] is True
    assert len(out["emails"]) == 5


def test_fresh_bypasses_cache(db_path):
    mc = mcache.MessageCache(recent_ttl=999)
    mc.set_recent("INBOX", _emails(5), now=1000.0)
    seen = {"n": 0}

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen["n"] += 1
        return _emails(3)
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=5, fresh=True,
        mc=mc, fetch_fn=fake_fetch, folders_fn=_only(["INBOX"]), now=1000.0)
    assert seen["n"] == 1                 # went live despite warm cache
    assert out["from_cache"] is False


# ---- new: send dedup relaxations -------------------------------------------------

def _ok_send(*a, **k):
    return "<m@x>"


def test_allow_duplicate_permits_distinct_message(db_path):
    sent = []

    def fake(acc, to, subject, body, attachments=None):
        sent.append(subject)
        return "<m@x>"
    r1 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                              tags=None, send_fn=fake, now=1000.0)
    assert r1["status"] == "sent"
    # default would BLOCK (recipient-only); allow_duplicate lets the distinct one through
    r2 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="B", body="b",
                              tags=None, allow_duplicate=True, send_fn=fake, now=1000.0 + 30)
    assert r2["status"] == "sent"
    assert len(sent) == 2


def test_allow_duplicate_still_blocks_exact_repeat(db_path):
    email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                         tags=None, allow_duplicate=True, send_fn=_ok_send, now=1000.0)
    r2 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                              tags=None, allow_duplicate=True, send_fn=_ok_send, now=1030.0)
    assert r2["status"] == "BLOCKED"


def test_relaxed_send_does_not_arm_recipient_block(db_path):
    # An allow_duplicate send must NOT record the recipient-only key, so a later STRICT
    # send to the same recipient is not phantom-blocked (the record/check-kinds fix).
    email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                         tags=None, allow_duplicate=True, send_fn=_ok_send, now=1000.0)
    r2 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="B", body="b",
                              tags=None, send_fn=_ok_send, now=1030.0)   # strict default
    assert r2["status"] == "sent"


def test_render_returns_copy_not_cache_alias(db_path):
    mc = mcache.MessageCache(recent_ttl=999)
    mc.set_recent("INBOX", _emails(2), now=1000.0)

    def boom(*a, **k):
        raise AssertionError("served from cache; no fetch expected")
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=2,
        mc=mc, fetch_fn=boom, folders_fn=_only(["INBOX"]), now=1000.0)
    out["emails"][0]["body"] = "MUTATED"          # caller mutates the response
    again = mc.get_recent("INBOX", 1, now=1000.0)
    assert again[0]["body"] != "MUTATED"          # cache untouched


def test_idempotency_key_blocks_only_same_key(db_path):
    r1 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                              tags=None, idempotency_key="k1", send_fn=_ok_send, now=1000.0)
    assert r1["status"] == "sent"
    r2 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="A", body="a",
                              tags=None, idempotency_key="k2", send_fn=_ok_send, now=1010.0)
    assert r2["status"] == "sent"                 # different key -> allowed
    r3 = email_ops.send_email(db_path, ACC, to=["x@y.com"], subject="Z", body="z",
                              tags=None, idempotency_key="k1", send_fn=_ok_send, now=1020.0)
    assert r3["status"] == "BLOCKED"              # k1 reused -> blocked
