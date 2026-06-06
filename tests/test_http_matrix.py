import asyncio
import json

import httpx
import pytest

from email_mcp.http import db
from email_mcp.http.matrix import MatrixBot

MK = "test-master-key"


class FakeSynapse:
    """Just enough of the client-server API for the bot: register (with username
    collisions), whoami, createRoom, join, joined_members, send, sync."""

    def __init__(self, used=()):
        self.used = set(used)
        self.tokens = set()
        self.register_calls = 0
        self.sent = []                   # (room_id, body text)
        self.joined = []                 # room_ids the bot joined
        self.members = {}                # room_id -> [user ids]
        self.sync_responses = []         # popped front-first; empty -> quiet sync

    def handler(self, request):
        p = request.url.path
        authed = request.headers.get("authorization", "").removeprefix("Bearer ") \
            in self.tokens
        if p.endswith("/register"):
            self.register_calls += 1
            body = json.loads(request.content)
            if body["username"] in self.used:
                return httpx.Response(400, json={"errcode": "M_USER_IN_USE"})
            self.used.add(body["username"])
            tok = f"tok-{self.register_calls}"
            self.tokens.add(tok)
            return httpx.Response(200, json={
                "user_id": f"@{body['username']}:chat.test",
                "access_token": tok, "device_id": "DEV"})
        if not authed:
            return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
        if p.endswith("/account/whoami"):
            return httpx.Response(200, json={"user_id": "@emailer:chat.test"})
        if p.endswith("/createRoom"):
            room = f"!dm{len(self.members) + 1}:chat.test"
            self.members[room] = ["@emailer:chat.test"]
            return httpx.Response(200, json={"room_id": room})
        if p.endswith("/join"):
            room = p.split("/rooms/")[1].split("/")[0]
            self.joined.append(room)
            return httpx.Response(200, json={"room_id": room})
        if p.endswith("/joined_members"):
            room = p.split("/rooms/")[1].split("/")[0]
            joined = {m: {} for m in self.members.get(room, [])}
            return httpx.Response(200, json={"joined": joined})
        if "/send/m.room.message/" in p:
            room = p.split("/rooms/")[1].split("/")[0]
            self.sent.append((room, json.loads(request.content)["body"]))
            return httpx.Response(200, json={"event_id": f"$e{len(self.sent)}"})
        if p.endswith("/sync"):
            if self.sync_responses:
                return httpx.Response(200, json=self.sync_responses.pop(0))
            return httpx.Response(200, json={"next_batch": "s-end", "rooms": {}})
        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"})


@pytest.fixture
def hdb(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    return p


def make_bot(hdb, fake):
    return MatrixBot(lambda: hdb, "http://synapse.test", "https://email-mcp.test",
                     username="emailer", clock=lambda: 1000.0,
                     transport=httpx.MockTransport(fake.handler))


def registered_bot(hdb, fake):
    """A bot with a live identity, skipping the register round-trip."""
    bot = make_bot(hdb, fake)
    bot.user_id = "@emailer:chat.test"
    bot.access_token = "tok-live"
    fake.tokens.add("tok-live")
    db.set_service_identity(hdb, "matrix_user", bot.user_id)
    return bot


# ---- registration ----------------------------------------------------------------------

def test_register_retries_taken_usernames_and_persists(hdb):
    fake = FakeSynapse(used={"emailer"})        # the base name is taken
    bot = make_bot(hdb, fake)
    uid = asyncio.run(bot.ensure_registered())
    assert uid == "@emailer-02:chat.test"
    assert db.get_service_identity(hdb, "matrix_user") == uid
    assert db.get_service_identity(hdb, "matrix_token") == bot.access_token
    assert db.get_service_identity(hdb, "matrix_password")

    # a fresh process loads the persisted identity instead of re-registering
    calls_before = fake.register_calls
    bot2 = make_bot(hdb, fake)
    assert asyncio.run(bot2.ensure_registered()) == uid
    assert fake.register_calls == calls_before


def test_dead_token_recovers_via_password_login(hdb):
    fake = FakeSynapse()
    bot = make_bot(hdb, fake)
    uid = asyncio.run(bot.ensure_registered())
    fake.tokens.discard(bot.access_token)       # token dies server-side

    def login_handler(request):
        if request.url.path.endswith("/login"):
            body = json.loads(request.content)
            assert body["password"] == db.get_service_identity(hdb, "matrix_password")
            fake.tokens.add("tok-relogin")
            return httpx.Response(200, json={"user_id": uid,
                                             "access_token": "tok-relogin"})
        return fake.handler(request)

    bot2 = MatrixBot(lambda: hdb, "http://synapse.test", "https://email-mcp.test",
                     username="emailer", clock=lambda: 1000.0,
                     transport=httpx.MockTransport(login_handler))
    assert asyncio.run(bot2.ensure_registered()) == uid
    assert bot2.access_token == "tok-relogin"
    assert db.get_service_identity(hdb, "matrix_token") == "tok-relogin"


# ---- commands ----------------------------------------------------------------------------

def test_login_command_mints_a_valid_token(hdb):
    bot = registered_bot(hdb, FakeSynapse())
    reply = bot.handle_command("@bob:chat.test", "login")
    assert "https://email-mcp.test/?token=" in reply and "24h" in reply
    raw = reply.split("?token=")[1].split()[0]
    uid = db.get_or_create_user(hdb, "@bob:chat.test", 1000.0)
    assert db.validate_login_token(hdb, raw, now=1500.0) == uid


def test_partial_command_suggests_usage(hdb):
    bot = registered_bot(hdb, FakeSynapse())
    reply = bot.handle_command("@bob:chat.test", "log")
    assert "Did you mean" in reply and "`login`" in reply and "`logout`" in reply
    reply = bot.handle_command("@bob:chat.test", "stat")
    assert "Did you mean `status`" in reply
    # something entirely unknown falls back to help
    assert "service bot" in bot.handle_command("@bob:chat.test", "wat")


def test_logout_disconnects_everything(hdb):
    bot = registered_bot(hdb, FakeSynapse())
    now = 1000.0
    uid = db.get_or_create_user(hdb, "@bob:chat.test", now)
    mid = db.create_mailbox(hdb, uid, "p", "bob@x.com", "i", 993, "s", 587, "pw",
                            now=now, master_key=MK)
    sess = db.create_session(hdb, uid, now)
    link = db.issue_login_token(hdb, uid, now)

    reply = bot.handle_command("@bob:chat.test", "logout")
    assert "1 mailbox(es) disconnected" in reply
    assert db.get_mailbox(hdb, mid)["enc_password"] is None
    assert db.validate_session(hdb, sess, now=now + 1) is None
    assert db.validate_login_token(hdb, link, now=now + 1) is None


def test_status_command(hdb):
    bot = registered_bot(hdb, FakeSynapse())
    assert "No mailboxes yet" in bot.handle_command("@bob:chat.test", "status")
    uid = db.get_or_create_user(hdb, "@bob:chat.test", 1000.0)
    mid = db.create_mailbox(hdb, uid, "p", "bob@x.com", "i", 993, "s", 587, "pw",
                            now=1000.0, master_key=MK)
    _, tid = db.create_auth_token(hdb, mid, "agent", "read", "@bob:chat.test", 1000.0)
    db.bump_usage(hdb, tid, "read", now=1001.0, n=3)
    reply = bot.handle_command("@bob:chat.test", "status")
    assert "bob@x.com" in reply and "connected" in reply
    assert "1 active key(s) of 1" in reply and "3 reads" in reply


# ---- sync loop: invites + DM replies -----------------------------------------------------

def test_sync_joins_dm_invite_and_answers_command(hdb):
    fake = FakeSynapse()
    bot = registered_bot(hdb, fake)
    room = "!dm9:chat.test"
    fake.members[room] = ["@emailer:chat.test", "@bob:chat.test"]
    fake.sync_responses = [
        {"next_batch": "s1", "rooms": {"invite": {room: {"invite_state": {"events": [
            {"type": "m.room.member", "state_key": "@emailer:chat.test",
             "sender": "@bob:chat.test", "content": {"is_direct": True}}]}}}}},
        {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {"events": [
            {"type": "m.room.message", "sender": "@bob:chat.test",
             "content": {"msgtype": "m.text", "body": "login"}}]}}}}},
    ]

    async def run():
        since = await bot.sync_once(None)
        assert since == "s1" and fake.joined == [room]
        await bot.sync_once(since)
    asyncio.run(run())

    [(sent_room, body)] = fake.sent
    assert sent_room == room and "?token=" in body
    # the DM room is remembered for notifications
    assert db.get_service_identity(hdb, "dm_room::@bob:chat.test") == room


def test_sync_ignores_groups_and_own_messages(hdb):
    fake = FakeSynapse()
    bot = registered_bot(hdb, fake)
    group = "!grp:chat.test"
    fake.members[group] = ["@emailer:chat.test", "@bob:chat.test", "@eve:chat.test"]
    fake.sync_responses = [
        {"next_batch": "s1", "rooms": {"join": {group: {"timeline": {"events": [
            {"type": "m.room.message", "sender": "@bob:chat.test",
             "content": {"msgtype": "m.text", "body": "login"}},
            {"type": "m.room.message", "sender": "@emailer:chat.test",
             "content": {"msgtype": "m.text", "body": "login"}}]}}}}},
    ]
    asyncio.run(bot.sync_once(None))
    assert fake.sent == []                    # 3-member room: stay silent


def test_room_that_gains_a_member_stops_getting_sensitive_replies(hdb):
    """Privacy is decided per send, not from an invite-time cache: once a third
    member joins what used to be a DM, commands go unanswered there and
    notifications move to a fresh private room."""
    fake = FakeSynapse()
    bot = registered_bot(hdb, fake)
    room = "!dm9:chat.test"
    fake.members[room] = ["@emailer:chat.test", "@bob:chat.test"]
    fake.sync_responses = [
        {"next_batch": "s1", "rooms": {"invite": {room: {"invite_state": {"events": [
            {"type": "m.room.member", "state_key": "@emailer:chat.test",
             "sender": "@bob:chat.test", "content": {"is_direct": True}}]}}}}},
        {"next_batch": "s2", "rooms": {"join": {room: {"timeline": {"events": [
            {"type": "m.room.message", "sender": "@bob:chat.test",
             "content": {"msgtype": "m.text", "body": "login"}}]}}}}},
    ]

    async def run():
        since = await bot.sync_once(None)             # invite accepted, room cached
        fake.members[room].append("@eve:chat.test")   # ...then a third member joins
        await bot.sync_once(since)
    asyncio.run(run())
    assert fake.sent == []                            # no sign-in link posted there

    # notifications abandon the grown room and mint a fresh private one
    asyncio.run(bot.notify_user("@bob:chat.test", "saved!"))
    [(sent_room, body)] = fake.sent
    assert sent_room != room and body == "saved!"
    assert db.get_service_identity(hdb, "dm_room::@bob:chat.test") == sent_room


def test_sync_routes_reactions_to_the_approval_manager(hdb):
    fake = FakeSynapse()
    bot = registered_bot(hdb, fake)
    seen = []

    class StubMgr:
        async def on_reaction(self, ev):
            seen.append(ev)

        async def expire_overdue(self):
            pass

    bot.approvals = StubMgr()
    fake.sync_responses = [
        {"next_batch": "s1", "rooms": {"join": {"!r:chat.test": {"timeline":
            {"events": [
                {"type": "m.reaction", "sender": "@bob:chat.test",
                 "content": {"m.relates_to": {"rel_type": "m.annotation",
                                              "event_id": "$p1", "key": "👍"}}},
                {"type": "m.reaction", "sender": "@emailer:chat.test",  # our own
                 "content": {"m.relates_to": {"event_id": "$p1", "key": "👍"}}},
            ]}}}}},
    ]
    asyncio.run(bot.sync_once(None))
    assert len(seen) == 1                    # own reactions never dispatched
    assert seen[0]["sender"] == "@bob:chat.test"


def test_notify_user_creates_dm_and_never_raises(hdb):
    fake = FakeSynapse()
    bot = registered_bot(hdb, fake)
    asyncio.run(bot.notify_user("@bob:chat.test", "saved!"))
    [(room, body)] = fake.sent
    assert body == "saved!"
    # second notify reuses the persisted room
    asyncio.run(bot.notify_user("@bob:chat.test", "again"))
    assert fake.sent[1][0] == room

    # a dead homeserver is swallowed, not raised
    bot2 = MatrixBot(lambda: hdb, "http://synapse.test", "https://email-mcp.test",
                     transport=httpx.MockTransport(
                         lambda r: httpx.Response(500, json={})))
    bot2.user_id, bot2.access_token = "@emailer:chat.test", "x"
    asyncio.run(bot2.notify_user("@new:chat.test", "hi"))
