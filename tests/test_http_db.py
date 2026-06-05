import pytest

from email_mcp.http import db

MK = "test-master-key"


@pytest.fixture
def hdb(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    return p


def test_user_get_or_create_is_idempotent(hdb):
    a = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    b = db.get_or_create_user(hdb, "@bob:chat", now=2.0)
    assert a == b
    assert db.get_user(hdb, a)["matrix_user"] == "@bob:chat"


def test_mailbox_create_and_decrypt(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "primary", "bob@icloud.com",
                            "imap.x", 993, "smtp.x", 587, "app-pw",
                            policy={"allowed_recipients": [".*@x"]}, now=1.0, master_key=MK)
    acc = db.mailbox_account(hdb, mid, master_key=MK)
    assert acc["email"] == "bob@icloud.com" and acc["password"] == "app-pw"
    assert acc["name"] == f"mbx-{mid}"
    assert db.mailbox_policy_dict(hdb, mid) == {"allowed_recipients": [".*@x"]}


def test_unique_email_enforced(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                      now=1.0, master_key=MK)
    with pytest.raises(db.EmailTaken):
        db.create_mailbox(hdb, uid, "p2", "bob@icloud.com", "i", 993, "s", 587, "pw2",
                          now=2.0, master_key=MK)


def test_delete_tombstones_email_and_frees_it(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    db.delete_mailbox(hdb, mid, now=2.0, suffix="zz")
    # email freed: a fresh mailbox with the same address is allowed
    assert not db.email_in_use(hdb, "bob@icloud.com")
    mid2 = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw2",
                             now=3.0, master_key=MK)
    assert mid2 != mid
    assert db.get_mailbox(hdb, mid)["email"] == "bob@icloud.com#deleted-zz"


def test_logout_erases_password(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    db.erase_mailbox_password(hdb, mid)
    assert db.mailbox_account(hdb, mid, master_key=MK) is None


def test_login_token_lifecycle(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1000.0)
    raw = db.issue_login_token(hdb, uid, now=1000.0, ttl=100)
    assert db.validate_login_token(hdb, raw, now=1050.0) == uid
    assert db.validate_login_token(hdb, raw, now=1101.0) is None      # expired
    assert db.validate_login_token(hdb, "bogus", now=1050.0) is None


def test_login_token_consume_is_single_use(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1000.0)
    raw = db.issue_login_token(hdb, uid, now=1000.0, ttl=100)
    assert db.consume_login_token(hdb, raw, now=1050.0) == uid
    assert db.consume_login_token(hdb, raw, now=1050.0) is None       # already redeemed
    assert db.validate_login_token(hdb, raw, now=1050.0) is None      # dead everywhere
    # expired tokens can't be redeemed at all
    raw2 = db.issue_login_token(hdb, uid, now=1000.0, ttl=100)
    assert db.consume_login_token(hdb, raw2, now=1101.0) is None


def test_session_lifecycle(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1000.0)
    raw = db.create_session(hdb, uid, now=1000.0, ttl=100)
    assert db.validate_session(hdb, raw, now=1050.0) == uid
    assert db.validate_session(hdb, raw, now=1101.0) is None          # expired
    assert db.validate_session(hdb, "bogus", now=1050.0) is None
    raw2 = db.create_session(hdb, uid, now=1000.0, ttl=100)
    db.delete_session(hdb, raw2)
    assert db.validate_session(hdb, raw2, now=1050.0) is None         # signed out


def test_auth_token_create_resolve_usage(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    raw, tid = db.create_auth_token(hdb, mid, "orchestrator", "read,send,mint",
                                    created_by="@bob:chat", now=1.0)
    p = db.principal_for_raw(hdb, raw)
    assert p["mailbox_id"] == mid and p["active"] and p["scopes"] == {"read", "send", "mint"}
    assert db.principal_for_raw(hdb, "nope") is None

    db.bump_usage(hdb, tid, "read", now=2.0)
    db.bump_usage(hdb, tid, "read", now=3.0)
    db.bump_usage(hdb, tid, "send", now=4.0)
    row = db.get_auth_token(hdb, tid)
    assert row["cnt_read"] == 2 and row["cnt_send"] == 1 and row["last_used_at"] == 4.0

    db.set_auth_token_active(hdb, tid, False)
    assert db.principal_for_raw(hdb, raw)["active"] is False


def test_invalid_scope_rejected(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    with pytest.raises(ValueError):
        db.create_auth_token(hdb, mid, "x", "read,fly", "@bob:chat", now=1.0)


def test_approval_lifecycle(hdb):
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@icloud.com", "i", 993, "s", 587, "pw",
                            now=1.0, master_key=MK)
    aid = db.create_approval(hdb, 5, mid, "x@y.com", "Hi", "body...",
                             payload={"to": ["x@y.com"]}, now=1.0)
    db.set_approval_event(hdb, aid, "$evt1")
    assert db.get_approval(hdb, aid)["matrix_event_id"] == "$evt1"
    assert [a["id"] for a in db.list_pending_approvals(hdb)] == [aid]
    db.resolve_approval(hdb, aid, "approved", now=2.0)
    assert db.get_approval(hdb, aid)["status"] == "approved"
    assert db.list_pending_approvals(hdb) == []


def test_service_identity_roundtrip(hdb):
    db.set_service_identity(hdb, "matrix_user", "@emailer:chat.surdi.in")
    db.set_service_identity(hdb, "matrix_user", "@emailer-x7:chat.surdi.in")  # upsert
    assert db.get_service_identity(hdb, "matrix_user") == "@emailer-x7:chat.surdi.in"
    assert db.get_service_identity(hdb, "missing") is None
