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


def test_query_filters_client_side(db_path):
    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        return [
            {"subject": "has needle", "body": "x", "from_address": "a", "to_address": "b",
             "message_id": "<1>", "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
            {"subject": "no match", "body": "y", "from_address": "a", "to_address": "b",
             "message_id": "<2>", "received_date": "2026-05-28T10:00:00+00:00", "folder": "INBOX"},
        ]
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query="needle", folders=["INBOX"],
        include_sent=False, strip_to_text=False, page=1, page_size=20,
        fetch_fn=fake_fetch, folders_fn=lambda a, connect_fn=None: ["INBOX"], now=1000.0)
    assert out["total_estimate"] == 1
    assert out["emails"][0]["message_id"] == "<1>"


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
