"""Matrix bot for the HTTP service (Phase 2): self-registers a service account on the
homeserver (creds persisted in service_identity so reboots don't re-register), long-polls
/sync, and answers DM commands — replacing the docker-exec admin CLI for onboarding:

    help    what can I do
    login   mint a 24h single-use dashboard sign-in link
    status  your mailboxes, agent keys and usage
    logout  disconnect mailboxes (erase stored passwords) + end dashboard sessions

Plain httpx against the Matrix client-server API (no SDK dependency). Everything is
injectable for tests: the transport (httpx.MockTransport), the clock, the username base.
Commands are DM-only; the bot joins any room it's invited to but stays silent in rooms
with more than two members.
"""
import asyncio
import os
import time

import httpx

from email_mcp.http import db

V3 = "/_matrix/client/v3"


class MatrixBot:
    def __init__(self, db_path_fn, homeserver, service_base, username=None,
                 clock=time.time, transport=None, sync_timeout_ms=30000):
        self.db_path_fn = db_path_fn
        self.homeserver = homeserver.rstrip("/")
        self.service_base = service_base.rstrip("/")
        self.username = (username
                         or os.environ.get("EMAIL_MCP_MATRIX_USERNAME", "emailer"))
        self.clock = clock
        self.sync_timeout_ms = sync_timeout_ms
        self.user_id = None
        self.access_token = None
        self._dm_peers = {}                  # room_id -> peer (confirmed DM rooms)
        self._txn = 0
        self._client = httpx.AsyncClient(base_url=self.homeserver,
                                         transport=transport,
                                         timeout=httpx.Timeout(70.0))

    # ---- identity (register once, persist, survive token loss) ---------------------

    def _auth(self):
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _whoami_ok(self):
        try:
            r = await self._client.get(f"{V3}/account/whoami", headers=self._auth())
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _password_login(self):
        """Recover from a dead access token using the persisted password."""
        dbp = self.db_path_fn()
        password = db.get_service_identity(dbp, "matrix_password")
        if not (self.user_id and password):
            return False
        r = await self._client.post(f"{V3}/login", json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.user_id},
            "password": password,
            "initial_device_display_name": "email-mcp service"})
        if r.status_code != 200:
            return False
        body = r.json()
        self.access_token = body["access_token"]
        db.set_service_identity(dbp, "matrix_token", self.access_token)
        return True

    async def _register(self):
        """Self-register: `emailer`, then `emailer-02`.. on M_USER_IN_USE."""
        dbp = self.db_path_fn()
        for attempt in range(1, 51):
            candidate = (self.username if attempt == 1
                         else f"{self.username}-{attempt:02d}")
            password = db.new_token()
            payload = {"username": candidate, "password": password,
                       "auth": {"type": "m.login.dummy"},
                       "initial_device_display_name": "email-mcp service"}
            r = await self._client.post(f"{V3}/register", json=payload)
            body = r.json()
            if r.status_code == 401 and body.get("session"):
                # interactive-auth handshake: echo the session with the dummy stage
                payload["auth"]["session"] = body["session"]
                r = await self._client.post(f"{V3}/register", json=payload)
                body = r.json()
            if r.status_code == 200:
                self.user_id = body["user_id"]
                self.access_token = body["access_token"]
                db.set_service_identity(dbp, "matrix_user", self.user_id)
                db.set_service_identity(dbp, "matrix_token", self.access_token)
                db.set_service_identity(dbp, "matrix_device", body.get("device_id", ""))
                db.set_service_identity(dbp, "matrix_password", password)
                return self.user_id
            if body.get("errcode") in ("M_USER_IN_USE", "M_EXCLUSIVE"):
                continue
            raise RuntimeError(f"matrix register failed: {r.status_code} {body}")
        raise RuntimeError("matrix register failed: no free username in 50 attempts")

    async def ensure_registered(self):
        dbp = self.db_path_fn()
        self.user_id = db.get_service_identity(dbp, "matrix_user")
        self.access_token = db.get_service_identity(dbp, "matrix_token")
        if self.access_token and await self._whoami_ok():
            return self.user_id
        if await self._password_login():
            return self.user_id
        return await self._register()

    # ---- sending --------------------------------------------------------------------

    async def _send(self, room_id, text):
        self._txn += 1
        txn = f"emcp{int(self.clock() * 1000)}-{self._txn}"
        r = await self._client.put(
            f"{V3}/rooms/{room_id}/send/m.room.message/{txn}",
            headers=self._auth(), json={"msgtype": "m.text", "body": text})
        r.raise_for_status()
        return r.json().get("event_id")

    async def _dm_room_for(self, user_id):
        dbp = self.db_path_fn()
        key = f"dm_room::{user_id}"
        room = db.get_service_identity(dbp, key)
        if room:
            return room
        r = await self._client.post(f"{V3}/createRoom", headers=self._auth(), json={
            "is_direct": True, "invite": [user_id],
            "preset": "trusted_private_chat"})
        r.raise_for_status()
        room = r.json()["room_id"]
        db.set_service_identity(dbp, key, room)
        self._dm_peers[room] = user_id
        return room

    async def notify_user(self, user_id, text):
        """Best-effort DM (e.g. dashboard-save confirmations); never raises."""
        try:
            await self._send(await self._dm_room_for(user_id), text)
        except Exception:
            pass

    # ---- sync loop ------------------------------------------------------------------

    async def start(self):
        """Register + sync forever. Errors back off exponentially, never crash out."""
        backoff = 1
        while True:
            try:
                await self.ensure_registered()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        since, backoff = None, 1
        while True:
            try:
                since = await self.sync_once(since)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def sync_once(self, since=None):
        params = {"timeout": self.sync_timeout_ms,
                  "filter": '{"room":{"timeline":{"limit":10}}}'}
        if since:
            params["since"] = since
        r = await self._client.get(f"{V3}/sync", headers=self._auth(), params=params)
        if r.status_code == 401:                 # token died underneath us
            await self.ensure_registered()
            return since
        r.raise_for_status()
        data = r.json()
        rooms = data.get("rooms") or {}
        for room_id, inv in (rooms.get("invite") or {}).items():
            await self._accept_invite(room_id, inv)
        for room_id, joined in (rooms.get("join") or {}).items():
            for ev in ((joined.get("timeline") or {}).get("events") or []):
                await self._handle_event(room_id, ev)
        return data.get("next_batch") or since

    async def _accept_invite(self, room_id, invite):
        events = (invite.get("invite_state") or {}).get("events") or []
        inviter, direct = None, False
        for ev in events:
            if (ev.get("type") == "m.room.member"
                    and ev.get("state_key") == self.user_id):
                inviter = ev.get("sender")
                direct = bool((ev.get("content") or {}).get("is_direct"))
        try:
            await self._client.post(f"{V3}/rooms/{room_id}/join",
                                    headers=self._auth(), json={})
        except httpx.HTTPError:
            return
        if direct and inviter:
            self._dm_peers[room_id] = inviter
            db.set_service_identity(self.db_path_fn(), f"dm_room::{inviter}", room_id)

    async def _is_dm(self, room_id):
        if room_id in self._dm_peers:
            return True
        try:
            r = await self._client.get(f"{V3}/rooms/{room_id}/joined_members",
                                       headers=self._auth())
            members = list((r.json().get("joined") or {}).keys())
        except httpx.HTTPError:
            return False
        if len(members) <= 2:
            peer = next((m for m in members if m != self.user_id), None)
            self._dm_peers[room_id] = peer
            return True
        return False

    async def _handle_event(self, room_id, ev):
        if ev.get("type") != "m.room.message":
            return
        sender = ev.get("sender")
        if not sender or sender == self.user_id:
            return
        body = ((ev.get("content") or {}).get("body") or "").strip()
        if not body or not await self._is_dm(room_id):
            return
        await self._send(room_id, self.handle_command(sender, body))

    # ---- commands (pure-ish: DB side effects, returns the reply text) ---------------

    def handle_command(self, sender, body):
        word = body.split()[0].lower()
        handlers = {"help": self._cmd_help, "login": self._cmd_login,
                    "logout": self._cmd_logout, "status": self._cmd_status}
        if word in handlers:
            return handlers[word](sender)
        matches = [c for c in handlers if c.startswith(word)]
        if matches:
            usage = {"help": "help — list commands",
                     "login": "login — get a dashboard sign-in link (24h, single use)",
                     "logout": "logout — disconnect mailboxes + end sessions",
                     "status": "status — your mailboxes, keys and usage"}
            return ("Did you mean " + " or ".join(f"`{m}`" for m in matches) + "?\n"
                    + "\n".join(usage[m] for m in matches))
        return self._cmd_help(sender)

    def _cmd_help(self, sender):
        return ("I'm the email-mcp service bot 📬  DM me one of:\n"
                "  login  — get a dashboard sign-in link (24h, single use)\n"
                "  status — your mailboxes, agent keys and usage\n"
                "  logout — disconnect mailboxes + end dashboard sessions\n"
                "  help   — this text\n"
                f"Dashboard: {self.service_base}  ·  API docs: "
                f"{self.service_base}/info")

    def _cmd_login(self, sender):
        dbp, now = self.db_path_fn(), self.clock()
        uid = db.get_or_create_user(dbp, sender, now)
        raw = db.issue_login_token(dbp, uid, now)
        return ("Here's your dashboard sign-in link — valid 24h, works once:\n"
                f"{self.service_base}/?token={raw}\n"
                "Until the link is opened, the value after ?token= also works as a "
                f"REST Bearer credential (see {self.service_base}/info).")

    def _cmd_logout(self, sender):
        dbp, now = self.db_path_fn(), self.clock()
        uid = db.get_or_create_user(dbp, sender, now)
        n = 0
        for m in db.list_mailboxes_for_user(dbp, uid):
            if m["enc_password"]:
                db.erase_mailbox_password(dbp, m["id"])
                n += 1
        db.delete_sessions_for_user(dbp, uid)
        db.consume_login_tokens_for_user(dbp, uid)
        return (f"Done — {n} mailbox(es) disconnected (stored passwords erased), "
                "dashboard sessions ended, unused sign-in links voided. Agent keys "
                "remain but get mailbox_unavailable until you reconnect — send "
                "`login` to set up again.")

    def _cmd_status(self, sender):
        dbp, now = self.db_path_fn(), self.clock()
        uid = db.get_or_create_user(dbp, sender, now)
        mailboxes = db.list_mailboxes_for_user(dbp, uid)
        if not mailboxes:
            return ("No mailboxes yet — send `login` and connect one on the "
                    "dashboard.")
        lines = []
        for m in mailboxes:
            state = "connected" if m["enc_password"] else "disconnected"
            tokens = db.list_auth_tokens(dbp, m["id"])
            live = [t for t in tokens if t["active"] and not t["revoked"]]
            use = {f: sum(t[f"cnt_{f}"] for t in tokens)
                   for f in ("read", "search", "send", "blocked")}
            lines.append(
                f"{m['email']} — {state} · {len(live)} active key(s) of "
                f"{len(tokens)} · usage: {use['read']} reads, {use['search']} "
                f"searches, {use['send']} sends, {use['blocked']} blocked")
        return "\n".join(lines)

    async def aclose(self):
        await self._client.aclose()
