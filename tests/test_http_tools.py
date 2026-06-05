import pytest

from email_mcp.http import auth, db, tools

MK = "test-master-key"


@pytest.fixture
def hdb(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    return p


@pytest.fixture
def setup(hdb):
    """A user + mailbox + two keys: full (read,write,send) and read-only."""
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(
        hdb, uid, "primary", "bob@x.com", "imap.x", 993, "smtp.x", 587, "app-pw",
        policy={"allowed_recipients": ["ok@x\\.com"]}, now=1.0, master_key=MK)
    full_raw, full_id = db.create_auth_token(hdb, mid, "orchestrator",
                                             "read,write,send", "@bob:chat", now=1.0)
    ro_raw, ro_id = db.create_auth_token(hdb, mid, "reader", "read",
                                         "@bob:chat", now=1.0)
    return {"mid": mid, "full": (full_raw, full_id), "ro": (ro_raw, ro_id)}


@pytest.fixture
def bind(hdb):
    """Bind a principal (by raw key) to the ContextVar for the test's duration."""
    tokens = []

    def _bind(raw):
        principal = db.principal_for_raw(hdb, raw)
        tokens.append(auth.current_principal.set(principal))
        return principal

    yield _bind
    for t in reversed(tokens):
        auth.current_principal.reset(t)


def fake_folders(acc, connect_fn=None):
    return ["INBOX", "Junk"]


def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
    return [{"message_id": "<m1@x>", "subject": "hello", "body": "b",
             "from_address": "a@x.com", "to_address": acc["email"],
             "attachments": [], "received_date": "2026-06-05T00:00:00+00:00",
             "folder": folder, "uid": 1, "uidvalidity": 9}]


# ---- scope enforcement ---------------------------------------------------------------

def test_unauthenticated_tool_call(hdb):
    out = tools.tool_get_emails(hdb, MK)
    assert out["error"] == "unauthenticated"


def test_read_only_key_cannot_send_or_write(hdb, setup, bind):
    bind(setup["ro"][0])
    assert tools.tool_send_email(hdb, MK, to=["ok@x.com"], subject="s",
                                 body="b")["error"] == "forbidden"
    assert tools.tool_mark_email(hdb, MK, "<m@x>", read=True)["error"] == "forbidden"
    assert tools.tool_move_email(hdb, MK, "<m@x>", "Archive")["error"] == "forbidden"
    # and no usage was recorded for denied calls
    assert db.get_auth_token(hdb, setup["ro"][1])["cnt_send"] == 0


# ---- read path -----------------------------------------------------------------------

def test_get_emails_reads_and_meters(hdb, setup, bind):
    bind(setup["full"][0])
    out = tools.tool_get_emails(hdb, MK, now=10.0, fetch_fn=fake_fetch,
                                folders_fn=fake_folders)
    assert out["emails"][0]["subject"] == "hello"
    out = tools.tool_get_emails(hdb, MK, query="hello", now=11.0,
                                fetch_fn=fake_fetch, folders_fn=fake_folders)
    assert out["total_estimate"] >= 1
    row = db.get_auth_token(hdb, setup["full"][1])
    assert row["cnt_read"] == 1 and row["cnt_search"] == 1   # default vs query call


def test_list_accounts_reports_connection(hdb, setup, bind):
    bind(setup["full"][0])
    accs = tools.tool_list_accounts(hdb, MK)
    assert accs[0]["email"] == "bob@x.com" and accs[0]["connected"] is True
    db.erase_mailbox_password(hdb, setup["mid"])
    accs = tools.tool_list_accounts(hdb, MK)
    assert accs[0]["connected"] is False


def test_logged_out_mailbox_blocks_ops(hdb, setup, bind):
    bind(setup["full"][0])
    db.erase_mailbox_password(hdb, setup["mid"])
    out = tools.tool_get_emails(hdb, MK, fetch_fn=fake_fetch, folders_fn=fake_folders)
    assert out["error"] == "mailbox_unavailable"


# ---- send path -----------------------------------------------------------------------

def test_send_allowed_and_metered(hdb, setup, bind):
    bind(setup["full"][0])
    sent = []

    def fake_send(acc, to, subject, body, attachments=None):
        sent.append((acc["email"], to))
        return "<mid@x>"

    out = tools.tool_send_email(hdb, MK, to=["ok@x.com"], subject="s", body="b",
                                now=100.0, send_fn=fake_send)
    assert out["status"] == "sent"
    assert sent == [("bob@x.com", ["ok@x.com"])]
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_send"] == 1


def test_send_policy_block_meters_blocked(hdb, setup, bind):
    bind(setup["full"][0])

    def fake_send(acc, to, subject, body, attachments=None):
        raise AssertionError("must not send")

    out = tools.tool_send_email(hdb, MK, to=["evil@untrusted-domain.example"],
                                subject="s", body="b", now=100.0, send_fn=fake_send)
    assert out["status"] == "BLOCKED" and out["reason"] == "recipient_not_allowed"
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_blocked"] == 1


# ---- write path ----------------------------------------------------------------------

class FakeIMAPLocate:
    def __init__(self):
        self.mutations = []

    def select(self, folder, readonly=False):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return ("OK", [b"7"])
        self.mutations.append((command, args))
        return ("OK", [b"done"])

    def expunge(self):
        return ("OK", [b""])

    def logout(self):
        pass


def test_mark_and_move_with_write_scope(hdb, setup, bind):
    bind(setup["full"][0])
    fake = FakeIMAPLocate()
    out = tools.tool_mark_email(hdb, MK, "<m@x>", read=True, folders=["INBOX"],
                                connect_fn=lambda acc: fake)
    assert out.get("read") is True
    out = tools.tool_move_email(hdb, MK, "<m@x>", "Archive", folders=["INBOX"],
                                connect_fn=lambda acc: fake)
    assert out.get("dest_folder") == "Archive"
