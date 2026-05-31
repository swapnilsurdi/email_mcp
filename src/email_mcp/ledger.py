import hashlib
import json

from email_mcp import store

WINDOW_SECONDS = 10 * 60
KEY_KINDS = ("recipient", "recipient_subject", "recipient_body")


def _norm_recipients(recipients):
    return ",".join(sorted(r.strip().lower() for r in recipients))


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def compute_keys(recipients, subject, body):
    rec = _norm_recipients(recipients)
    return {
        "recipient": _h(rec),
        "recipient_subject": _h(rec + "\x00" + (subject or "")),
        "recipient_body": _h(rec + "\x00" + (body or "")),
    }


def _now_iso(now):
    # store epoch seconds as text for simple window math
    return str(now)


def record_queued(db_path, account, recipients, subject, body, tags, now):
    keys = compute_keys(recipients, subject, body)
    rec = _norm_recipients(recipients)
    tags_json = json.dumps(tags) if tags is not None else None
    ids = []
    with store.connect(db_path) as conn:
        for kind in KEY_KINDS:
            cur = conn.execute(
                "INSERT INTO send_ledger(account, key_kind, key_hash, recipients, "
                "subject_excerpt, status, tags, sent_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (account, kind, keys[kind], rec, (subject or "")[:120],
                 "queued", tags_json, _now_iso(now)),
            )
            ids.append(cur.lastrowid)
    return ids


def _update_status(db_path, ids, status, now, message_id=None):
    with store.connect(db_path) as conn:
        for i in ids:
            conn.execute(
                "UPDATE send_ledger SET status=?, message_id=? WHERE id=?",
                (status, message_id, i),
            )


def mark_sent(db_path, ids, message_id, now):
    _update_status(db_path, ids, "sent", now, message_id)


def mark_failed(db_path, ids, now):
    _update_status(db_path, ids, "failed", now)


def check_block(db_path, recipients, subject, body, now):
    keys = compute_keys(recipients, subject, body)
    cutoff = now - WINDOW_SECONDS
    matched = []
    prior = None
    with store.connect(db_path) as conn:
        for kind in KEY_KINDS:
            row = conn.execute(
                "SELECT recipients, status, tags, sent_at, message_id "
                "FROM send_ledger WHERE key_kind=? AND key_hash=? "
                "AND CAST(sent_at AS REAL) >= ? ORDER BY id DESC LIMIT 1",
                (kind, keys[kind], cutoff),
            ).fetchone()
            if row:
                matched.append(kind)
                if prior is None:
                    prior = {
                        "recipients": row[0],
                        "status": row[1],
                        "tags": json.loads(row[2]) if row[2] else None,
                        "sent_at": row[3],
                        "message_id": row[4],
                    }
    if not matched:
        return None
    return {
        "status": "BLOCKED",
        "matched": matched,
        "reason": "duplicate send within 10 min window",
        "prior": prior,
    }
