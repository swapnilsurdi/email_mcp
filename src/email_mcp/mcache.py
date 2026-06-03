"""In-memory message cache: a thread-safe LRU of parsed emails plus a per-folder
"recent" index, so the common "latest INBOX" reads (and repeat reads of the same
message) are served from RAM instead of a fresh IMAP round-trip.

Design notes (see docs/2026-05-28-email-mcp-design.md update + TODO-http-shared-server):
- It caches CONTENT only (headers, extracted text body, attachment METADATA) — never
  attachment bytes. Bytes are always re-fetched from IMAP on download, so the cache
  stays small and bounded regardless of attachment sizes.
- Dual key. The cache key is the **Message-ID** when present — it is immutable and
  survives the message being moved between folders (RFC 5322 §3.6.4), so cached content
  stays valid across moves. When a message has no Message-ID (or it would collide), we
  fall back to a synthetic **uid:<folder>:<uidvalidity>:<uid>** key — the IMAP-stable
  handle within a folder. Acting on a specific message (download/mark/move) always
  re-resolves against IMAP, so a duplicate Message-ID can at worst show slightly stale
  display content, never mutate the wrong message.
- Bounded by BOTH an entry count and a byte budget (whichever trips first), and each
  cached body is truncated, so a pathological large mailbox can't blow up RSS.
- Process-wide and thread-safe so the same instance can back the prefetch daemon and,
  later, a shared HTTP server for the whole agent fleet.
"""
import threading
from collections import OrderedDict


def cache_key(entry):
    """Stable key for an email dict: real Message-ID, else uid:folder:uidvalidity:uid."""
    mid = entry.get("message_id")
    if mid:
        return mid
    return "uid:%s:%s:%s" % (
        entry.get("folder"), entry.get("uidvalidity"), entry.get("uid"))


class MessageCache:
    def __init__(self, max_entries=256, max_bytes=32 * 1024 * 1024,
                 body_max=64 * 1024, recent_ttl=180):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.body_max = body_max
        self.recent_ttl = recent_ttl
        self._lock = threading.RLock()
        self._entries = OrderedDict()   # key -> (entry, size); LRU order
        self._bytes = 0
        self._recent = {}               # folder -> {"keys": [...], "ts": float}

    # ---- internals ------------------------------------------------------------
    def _prepare(self, entry):
        """Trim the cached body to the byte budget; return (entry, size_estimate)."""
        body = entry.get("body")
        if body and len(body) > self.body_max:
            entry = {**entry, "body": body[:self.body_max],
                     "body_truncated_in_cache": True}
        size = len(entry.get("body") or "") + 512  # body + rough header/meta overhead
        return entry, size

    def _evict_if_needed(self):
        while self._entries and (
                len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
            _, (_, size) = self._entries.popitem(last=False)   # drop least-recent
            self._bytes -= size

    def _put(self, entry):
        key = cache_key(entry)
        entry, size = self._prepare(entry)
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        self._entries[key] = (entry, size)   # inserted as most-recent
        self._bytes += size
        self._evict_if_needed()
        return key

    # ---- public API -----------------------------------------------------------
    def upsert(self, entries):
        """Add/refresh message entries in the LRU (content cache only)."""
        with self._lock:
            for e in entries:
                self._put(e)

    def set_recent(self, folder, entries, now):
        """Record `entries` (newest-first) as the authoritative latest view of `folder`,
        and upsert each into the LRU. Used by the prefetch poller and by a live
        latest-window fetch so a subsequent identical read is served from RAM."""
        with self._lock:
            keys = [self._put(e) for e in entries]
            self._recent[folder] = {"keys": keys, "ts": now}

    def get_recent(self, folder, n, now):
        """Return the newest `n` cached entries for `folder`, or None if the recent
        index is stale (> recent_ttl) or doesn't cover n still-resident entries."""
        with self._lock:
            rec = self._recent.get(folder)
            if not rec or (now - rec["ts"]) > self.recent_ttl:
                return None
            if len(rec["keys"]) < n:
                return None
            out = []
            for key in rec["keys"][:n]:
                item = self._entries.get(key)
                if item is None:           # evicted from under the index
                    return None
                self._entries.move_to_end(key)   # touch -> most-recent
                out.append(dict(item[0]))  # copy: the cached dict must not escape
            return out

    def get(self, key, now=None):
        """Look up one entry by cache key (e.g. a Message-ID). None if absent."""
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return None
            self._entries.move_to_end(key)
            return dict(item[0])           # copy: the cached dict must not escape

    def invalidate(self, key=None, folder=None):
        """Drop a single entry and/or a folder's recent index (call on mutate so other
        readers don't see stale state — used by the future shared HTTP server)."""
        with self._lock:
            if key is not None:
                item = self._entries.pop(key, None)
                if item is not None:
                    self._bytes -= item[1]
            if folder is not None:
                self._recent.pop(folder, None)

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "recent_folders": {f: len(r["keys"]) for f, r in self._recent.items()},
            }
