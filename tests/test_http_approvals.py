import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from email_mcp.http import app as http_app
from email_mcp.http import approvals as approvals_mod
from email_mcp.http import auth, db, tools

MK = "test-master-key"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeBot:
    def __init__(self):
        self.previews = []                  # (user, text)
        self.notes = []                     # (user, text)
        self._n = 0

    async def send_approval_preview(self, user_id, text):
        self._n += 1
        self.previews.append((user_id, text))
        return f"$preview{self._n}"

    async def notify_user(self, user_id, text):
        self.notes.append((user_id, text))


@pytest.fixture
def hdb(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    return p


@pytest.fixture
def setup(hdb):
    """@bob's mailbox with an allowlist (ok@x.com) and a blocklist (bad@x.com),
    plus a send-capable key and a read-only key."""
    uid = db.get_or_create_user(hdb, "@bob:chat", now=1.0)
    mid = db.create_mailbox(
        hdb, uid, "primary", "bob@x.com", "imap.x", 993, "smtp.x", 587, "app-pw",
        policy={"allowed_recipients": ["ok@x\\.com"],
                "blocked_recipients": ["bad@x\\.com"]}, now=1.0, master_key=MK)
    full_raw, full_id = db.create_auth_token(hdb, mid, "orch", "read,write,send",
                                             "@bob:chat", now=1.0)
    ro_raw, ro_id = db.create_auth_token(hdb, mid, "reader", "read",
                                         "@bob:chat", now=1.0)
    return {"uid": uid, "mid": mid, "full": (full_raw, full_id),
            "ro": (ro_raw, ro_id)}


@pytest.fixture
def bind(hdb):
    tokens = []

    def _bind(raw):
        principal = db.principal_for_raw(hdb, raw)
        tokens.append(auth.current_principal.set(principal))
        return principal

    yield _bind
    for t in reversed(tokens):
        auth.current_principal.reset(t)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mgr(hdb, clock):
    sent = []

    def fake_send(acc, to, subject, body, attachments=None):
        sent.append({"acc": acc["email"], "to": to, "subject": subject,
                     "body": body, "attachments": attachments})
        return "<sent-1@x>"

    m = approvals_mod.ApprovalManager(lambda: hdb, lambda: MK, clock=clock,
                                      send_fn=fake_send)
    m.bot = FakeBot()
    m.sent = sent
    return m


def make_pending(mgr, hdb, setup, to=("new@y.com",), subject="hi",
                 body="hello world"):
    """A pending approval whose preview has been delivered ($preview1)."""
    async def run():
        p = db.principal_for_raw(hdb, setup["full"][0])
        aid = mgr.request(p, to=list(to), subject=subject, body=body,
                          pending=list(to))
        await asyncio.sleep(0)               # let the scheduled preview task run
        return aid
    return asyncio.run(run())


def reaction(sender, key, target="$preview1"):
    return {"type": "m.reaction", "sender": sender,
            "content": {"m.relates_to": {"rel_type": "m.annotation",
                                         "event_id": target, "key": key}}}


# ---- the three tiers at the send tool ---------------------------------------------------

def test_blocklisted_recipient_is_blocked(hdb, setup, bind, mgr):
    bind(setup["full"][0])
    out = tools.tool_send_email(hdb, MK, to=["bad@x.com"], subject="s", body="b",
                                approvals=mgr)
    assert out["status"] == "BLOCKED" and out["reason"] == "recipient_blocked"
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_blocked"] == 1
    assert mgr.bot.previews == []            # nobody was asked


def test_allowlisted_recipient_sends_without_prompt(hdb, setup, bind, mgr):
    bind(setup["full"][0])
    sent = []

    def send_fn(acc, to, subject, body, attachments=None):
        sent.append(to)
        return "<m@x>"

    out = tools.tool_send_email(hdb, MK, to=["ok@x.com"], subject="s", body="b",
                                send_fn=send_fn, approvals=mgr)
    assert out["status"] == "sent" and sent == [["ok@x.com"]]
    assert mgr.bot.previews == []


def test_unknown_recipient_without_channel_is_blocked(hdb, setup, bind, clock):
    bind(setup["full"][0])
    bare = approvals_mod.ApprovalManager(lambda: hdb, lambda: MK, clock=clock)
    assert not bare.available()              # no bot wired
    out = tools.tool_send_email(hdb, MK, to=["new@y.com"], subject="s", body="b",
                                approvals=bare)
    assert out["status"] == "BLOCKED" and out["reason"] == "recipient_not_allowed"


def test_unknown_recipient_goes_pending_and_previews_owner(hdb, setup, bind, mgr):
    bind(setup["full"][0])

    async def run():
        out = tools.tool_send_email(hdb, MK, to=["new@y.com"], subject="lunch?",
                                    body="how about thursday", approvals=mgr)
        await asyncio.sleep(0)
        return out
    out = asyncio.run(run())

    assert out["status"] == "pending_approval"
    assert out["pending_recipients"] == ["new@y.com"]
    [(owner, text)] = mgr.bot.previews
    assert owner == "@bob:chat"              # the mailbox owner, not the agent
    assert "new@y.com" in text and "lunch?" in text and "👍" in text
    row = db.get_approval(hdb, out["approval_id"])
    assert row["status"] == "pending" and row["matrix_event_id"] == "$preview1"
    assert mgr.sent == []                    # nothing went out yet


# ---- decisions ---------------------------------------------------------------------------

def test_thumbs_up_from_owner_sends(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    clock.t += 60                            # within the 180s window
    asyncio.run(mgr.on_reaction(reaction("@bob:chat", "👍")))

    row = db.get_approval(hdb, aid)
    assert row["status"] == "approved"
    [send] = mgr.sent
    assert send["to"] == ["new@y.com"] and send["acc"] == "bob@x.com"
    assert json.loads(row["payload_json"])["result"]["status"] == "sent"
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_send"] == 1
    assert any("✅" in t for _, t in mgr.bot.notes)


def test_other_reaction_rejects(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    asyncio.run(mgr.on_reaction(reaction("@bob:chat", "❌")))
    assert db.get_approval(hdb, aid)["status"] == "rejected"
    assert mgr.sent == []
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_blocked"] == 1


def test_reaction_from_anyone_else_is_ignored(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    asyncio.run(mgr.on_reaction(reaction("@eve:chat", "👍")))
    assert db.get_approval(hdb, aid)["status"] == "pending"
    assert mgr.sent == []


def test_late_thumbs_up_expires_instead_of_sending(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    clock.t += 181                           # past the deadline
    asyncio.run(mgr.on_reaction(reaction("@bob:chat", "👍")))
    assert db.get_approval(hdb, aid)["status"] == "expired"
    assert mgr.sent == []


def test_expire_overdue_sweep_notifies_owner(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    clock.t += 181
    asyncio.run(mgr.expire_overdue())
    assert db.get_approval(hdb, aid)["status"] == "expired"
    assert any("⏰" in t for _, t in mgr.bot.notes)
    assert db.get_auth_token(hdb, setup["full"][1])["cnt_blocked"] == 1


def test_blocklist_change_beats_a_thumbs_up(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    db.set_mailbox_policy(hdb, setup["mid"],
                          {"blocked_recipients": ["new@y\\.com"]})
    asyncio.run(mgr.on_reaction(reaction("@bob:chat", "👍")))
    assert db.get_approval(hdb, aid)["status"] == "rejected"
    assert mgr.sent == []


# ---- outcome queries -----------------------------------------------------------------------

def test_get_send_status_tool_scoping(hdb, setup, bind, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    bind(setup["ro"][0])                     # read scope may query
    view = tools.tool_get_send_status(hdb, MK, aid, approvals=mgr)
    assert view["approval_id"] == aid and view["status"] == "pending"

    # a key bound to a different mailbox sees nothing
    uid2 = db.get_or_create_user(hdb, "@carol:chat", now=1.0)
    mid2 = db.create_mailbox(hdb, uid2, "c", "carol@x.com", "i", 993, "s", 587,
                             "pw", now=1.0, master_key=MK)
    other_raw, _ = db.create_auth_token(hdb, mid2, "k", "read", "@carol:chat", 1.0)
    bind(other_raw)
    assert tools.tool_get_send_status(hdb, MK, aid,
                                      approvals=mgr)["error"] == "not_found"


def test_status_read_lazily_expires(hdb, setup, mgr, clock):
    aid = make_pending(mgr, hdb, setup)
    clock.t += 181
    row = mgr.status(aid)
    assert row["status"] == "expired"


def test_rest_approval_endpoint(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    mgr = approvals_mod.ApprovalManager(lambda: p, lambda: MK,
                                        clock=lambda: 1000.0)
    application = http_app.create_app(db_path_fn=lambda: p,
                                      master_key_fn=lambda: MK,
                                      base_url="https://email-mcp.test",
                                      approvals_mgr=mgr)
    with TestClient(application) as c:
        import time as _time
        uid = db.get_or_create_user(p, "@admin:chat", 1.0)
        t = db.issue_login_token(p, uid, _time.time())
        mid = db.create_mailbox(p, uid, "p", "a@x.com", "i", 993, "s", 587, "pw",
                                now=1.0, master_key=MK)
        key, tid = db.create_auth_token(p, mid, "k", "send", "@admin:chat", 1.0)
        aid = db.create_approval(p, tid, mid, "new@y.com", "hi", "preview",
                                 {"request": {}}, 1000.0)

        hdr = {"Authorization": f"Bearer {t}"}
        r = c.get(f"/api/approvals/{aid}", headers=hdr)
        assert r.status_code == 200 and r.json()["status"] == "pending"
        r = c.get(f"/api/approvals/{aid}",
                  headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200          # the agent key may poll too
        assert c.get(f"/api/approvals/{aid}").status_code == 401
        assert c.get("/api/approvals/9999", headers=hdr).status_code == 404
