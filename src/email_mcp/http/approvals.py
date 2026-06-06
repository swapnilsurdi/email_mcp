"""OTP send-approval (Phase 3): a send to a recipient that is neither allowlisted nor
blocklisted creates an `approvals` row, DMs the mailbox owner a preview on Matrix, and
returns `pending_approval` to the agent immediately (non-blocking). The bot's sync loop
feeds reactions back here: a 👍 from the owner within the deadline performs the real
send; any other reaction rejects; no reaction expires it. Agents learn the outcome via
the `get_send_status` tool or `GET /api/approvals/{id}`.

Everything is injectable for tests: the clock, the TTL, and the SMTP `send_fn` that is
passed straight through to the unchanged core `email_ops.send_email`.
"""
import asyncio
import json
import os
import time

from email_mcp import email_ops, security
from email_mcp.http import db

DEFAULT_TTL = 180


def approval_view(row):
    """The shape agents see (tool + REST): status, recipients, outcome."""
    payload = json.loads(row["payload_json"] or "{}")
    return {"approval_id": row["id"], "status": row["status"],
            "recipients": row["recipient"], "subject": row["subject"],
            "requested_at": row["requested_at"], "decided_at": row["decided_at"],
            "result": payload.get("result")}


class ApprovalManager:
    def __init__(self, db_path_fn, master_key_fn, clock=time.time, ttl=None,
                 send_fn=None):
        self.db_path_fn = db_path_fn
        self.master_key_fn = master_key_fn
        self.clock = clock
        self.ttl = ttl or int(os.environ.get("EMAIL_MCP_APPROVAL_TTL", DEFAULT_TTL))
        self.send_fn = send_fn               # test double; None = real SMTP
        self.bot = None                      # wired by the app factory
        self._loop = None                    # bound in lifespan for thread handoff

    def available(self):
        """Whether an approval can actually reach the owner. Without a bot the
        middle tier degrades to a plain deny (stdio semantics)."""
        return getattr(self.bot, "send_approval_preview", None) is not None

    def bind_loop(self, loop):
        self._loop = loop

    # ---- creation (called from the send tool, often on a worker thread) ------------

    def request(self, principal, to, subject, body, tags=None, attachments=None,
                allow_duplicate=False, idempotency_key=None, pending=None, now=None):
        """Create the row + schedule the preview DM. Returns the approval id."""
        now = self.clock() if now is None else now
        payload = {"request": {
            "to": list(to), "subject": subject, "body": body, "tags": tags,
            "attachments": attachments, "allow_duplicate": allow_duplicate,
            "idempotency_key": idempotency_key}}
        aid = db.create_approval(
            self.db_path_fn(), principal["token_id"], principal["mailbox_id"],
            ", ".join(pending or security.expand_recipients(to)),
            subject, (body or "")[:200], payload, now)
        self._schedule(self._deliver_preview(aid))
        return aid

    def _schedule(self, coro):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
            return
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()                     # no loop (bare unit test): drop quietly

    async def _deliver_preview(self, aid):
        """Best-effort: an undeliverable preview just lets the approval expire."""
        try:
            row = db.get_approval(self.db_path_fn(), aid)
            owner = self._owner_of(row)
            if owner is None or not self.available():
                return
            text = (f"📨 Send approval #{aid} — an agent wants to email "
                    f"{row['recipient']}\n"
                    f"Subject: {row['subject'] or '(none)'}\n"
                    f"Preview: {row['body_excerpt'] or '(empty)'}\n"
                    f"React 👍 within {self.ttl}s to send — any other reaction "
                    "rejects, no reaction expires it.")
            event_id = await self.bot.send_approval_preview(owner, text)
            if event_id:
                db.set_approval_event(self.db_path_fn(), aid, event_id)
        except Exception:
            pass

    # ---- decisions (driven by the bot's sync loop) ----------------------------------

    async def on_reaction(self, ev):
        rel = (ev.get("content") or {}).get("m.relates_to") or {}
        target, key = rel.get("event_id"), rel.get("key") or ""
        if not target:
            return
        dbp = self.db_path_fn()
        row = db.get_approval_by_event(dbp, target)
        if row is None or row["status"] != "pending":
            return
        if ev.get("sender") != self._owner_of(row):
            return                            # only the mailbox owner decides
        now = self.clock()
        if now > row["requested_at"] + self.ttl:
            await self._expire(row, now)
            return
        if key.startswith("👍"):              # startswith: tolerate U+FE0F variants
            await self._approve_and_send(row, now)
        else:
            db.resolve_approval(dbp, row["id"], "rejected", now)
            self._bump(row, "blocked", now)
            await self._notify_owner(
                row, f"🚫 Send #{row['id']} rejected — nothing was sent.")

    async def _approve_and_send(self, row, now):
        dbp = self.db_path_fn()
        payload = json.loads(row["payload_json"] or "{}")
        req = payload.get("request") or {}
        policy = security.policy_from_mapping(
            db.mailbox_policy_dict(dbp, row["mailbox_id"]))
        # the blocklist still wins, even over a 👍 (it may have changed since)
        blocked = [r for r, c in policy.classify_recipients(req.get("to") or [])
                   .items() if c == "block"]
        acc = None if blocked else db.mailbox_account(
            dbp, row["mailbox_id"], master_key=self.master_key_fn())
        if blocked or acc is None:
            db.resolve_approval(dbp, row["id"], "rejected", now)
            self._bump(row, "blocked", now)
            reason = ("recipient now on the blocklist" if blocked
                      else "the mailbox is disconnected")
            payload["result"] = {"status": "BLOCKED", "reason": reason}
            db.update_approval_payload(dbp, row["id"], payload)
            await self._notify_owner(
                row, f"🚫 Send #{row['id']} not sent — {reason}.")
            return
        kwargs = {"send_fn": self.send_fn} if self.send_fn else {}
        # policy=None: the human's 👍 IS the recipient authorization; the dedup
        # ledger still applies as usual inside send_email
        result = await asyncio.to_thread(
            email_ops.send_email, dbp, acc, to=req.get("to"),
            subject=req.get("subject"), body=req.get("body"),
            tags=req.get("tags"), attachments=req.get("attachments"),
            allow_duplicate=req.get("allow_duplicate", False),
            idempotency_key=req.get("idempotency_key"),
            policy=None, now=now, **kwargs)
        db.resolve_approval(dbp, row["id"], "approved", now)
        payload["result"] = result
        db.update_approval_payload(dbp, row["id"], payload)
        if (result or {}).get("status") == "sent":
            self._bump(row, "send", now)
            await self._notify_owner(row, f"✅ Send #{row['id']} approved and sent.")
        else:
            await self._notify_owner(
                row, f"⚠️ Send #{row['id']} approved but the send did not go "
                     f"through: {(result or {}).get('status', 'unknown')}.")

    async def expire_overdue(self):
        """Sweep pending approvals past the deadline (called per sync tick)."""
        now = self.clock()
        for row in db.list_pending_approvals(self.db_path_fn()):
            if now > row["requested_at"] + self.ttl:
                await self._expire(row, now)

    async def _expire(self, row, now):
        db.resolve_approval(self.db_path_fn(), row["id"], "expired", now)
        self._bump(row, "blocked", now)
        await self._notify_owner(
            row, f"⏰ Send approval #{row['id']} expired after {self.ttl}s — "
             "nothing was sent.")

    # ---- queries ---------------------------------------------------------------------

    def status(self, approval_id, now=None):
        """The row, lazily expiring an overdue pending one at read time."""
        now = self.clock() if now is None else now
        dbp = self.db_path_fn()
        row = db.get_approval(dbp, approval_id)
        if row and row["status"] == "pending" \
                and now > row["requested_at"] + self.ttl:
            db.resolve_approval(dbp, approval_id, "expired", now)
            self._bump(row, "blocked", now)
            row = db.get_approval(dbp, approval_id)
        return row

    # ---- helpers ---------------------------------------------------------------------

    def _owner_of(self, row):
        dbp = self.db_path_fn()
        mb = db.get_mailbox(dbp, row["mailbox_id"])
        if mb is None:
            return None
        user = db.get_user(dbp, mb["user_id"])
        return user["matrix_user"] if user else None

    def _bump(self, row, field, now):
        if row.get("auth_token_id"):
            db.bump_usage(self.db_path_fn(), row["auth_token_id"], field, now)

    async def _notify_owner(self, row, text):
        owner = self._owner_of(row)
        if owner and self.bot is not None:
            await self.bot.notify_user(owner, text)
