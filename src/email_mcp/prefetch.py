"""Background prefetch poller. A daemon thread that periodically pulls the latest N
messages of a folder (INBOX by default) into the shared MessageCache, so "latest
emails" reads are served from RAM. Read-only, BODY.PEEK[] — never marks mail read.

It is delta-driven: after the first full pull it only fetches messages with a UID
greater than the highest seen, so a quiet mailbox costs one cheap UID SEARCH per cycle
and no body download. On a UIDVALIDITY change (mailbox re-created server-side) it resets
and re-pulls fully.

Disabled by default (interval 0). Enabled via EMAIL_MCP_PREFETCH_INTERVAL; see
runtime.prefetch_config(). Designed to be the single shared poller behind a future HTTP
server (docs/TODO-http-shared-server.md)."""
import sys
import threading
import time

from email_mcp import mcache as _mcache
from email_mcp.providers import imap_account


def _dedupe(entries):
    seen, out = set(), []
    for e in entries:
        k = _mcache.cache_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _log(msg):
    # stderr so it lands in MCP server logs, never in tool output.
    print("[email-mcp prefetch] " + msg, file=sys.stderr, flush=True)


def run_cycle(state, account_fn, cache, folder, count, now,
              fetch_fn=imap_account.fetch_inbox_recent):
    """One prefetch pass. Mutates `state` ({uidvalidity, max_uid, entries}) and writes
    the merged latest view into `cache`. Returns the merged entries (for tests)."""
    acc = account_fn()
    since = state.get("max_uid")
    uv, mx, new_entries = fetch_fn(acc, folder, count, since_uid=since)
    prev_uv = state.get("uidvalidity")
    if prev_uv is not None and uv != prev_uv:
        # mailbox identity changed -> any delta is meaningless; refetch full and reset.
        # (prev_uv None is just the first cycle, NOT a change.)
        uv, mx, new_entries = fetch_fn(acc, folder, count, since_uid=None)
        state["entries"] = []
    if new_entries:
        merged = _dedupe(new_entries + state.get("entries", []))
        merged.sort(key=lambda m: m.get("received_date", ""), reverse=True)
        merged = merged[:count]
        state["entries"] = merged
    else:
        merged = state.get("entries", [])
    cache.set_recent(folder, merged, now)   # (re)stamp freshness even with no new mail
    state["uidvalidity"] = uv
    if mx is not None:
        state["max_uid"] = mx
    return merged


def _loop(account_fn, cache, folder, count, interval, stop_event, now_fn):
    state = {"uidvalidity": None, "max_uid": None, "entries": []}
    while not stop_event.is_set():
        try:
            run_cycle(state, account_fn, cache, folder, count, now_fn())
        except Exception as e:                  # never let the poller die on a blip
            _log("cycle error: %s" % e)
        stop_event.wait(interval)


def start(account_fn, cache, folder="INBOX", count=50, interval=120, now_fn=time.time):
    """Spawn the daemon poller. Returns (thread, stop_event). No-op (returns None) if
    interval <= 0."""
    if interval <= 0:
        return None
    stop_event = threading.Event()
    t = threading.Thread(
        target=_loop,
        args=(account_fn, cache, folder, count, interval, stop_event, now_fn),
        name="email-mcp-prefetch", daemon=True)
    t.start()
    _log("started: folder=%s count=%d interval=%ds" % (folder, count, interval))
    return (t, stop_event)
