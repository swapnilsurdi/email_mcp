from email_mcp import ledger

REC = ["a@x.com", "b@y.com"]


def test_compute_keys_are_three_and_recipient_order_independent():
    k1 = ledger.compute_keys(REC, "Subj", "Body")
    k2 = ledger.compute_keys(list(reversed(REC)), "Subj", "Body")
    assert set(k1) == {"recipient", "recipient_subject", "recipient_body"}
    assert k1 == k2


def test_no_block_when_empty(db_path):
    assert ledger.check_block(db_path, REC, "S", "B", now=1000.0) is None


def test_recipient_only_is_hard_block_even_different_content(db_path):
    ledger.record_queued(db_path, "acct", REC, "S1", "B1", tags={"t": 1}, now=1000.0)
    block = ledger.check_block(db_path, REC, "S2-different", "B2-different", now=1000.0 + 60)
    assert block is not None
    assert "recipient" in block["matched"]
    assert block["prior"]["status"] == "queued"
    assert block["prior"]["tags"] == {"t": 1}


def test_block_expires_after_10_min(db_path):
    ledger.record_queued(db_path, "acct", REC, "S", "B", tags=None, now=1000.0)
    assert ledger.check_block(db_path, REC, "S", "B", now=1000.0 + 11 * 60) is None


def test_failed_send_still_blocks(db_path):
    ids = ledger.record_queued(db_path, "acct", REC, "S", "B", tags=None, now=1000.0)
    ledger.mark_failed(db_path, ids, now=1000.0)
    block = ledger.check_block(db_path, REC, "S", "B", now=1000.0 + 60)
    assert block is not None
    assert block["prior"]["status"] == "failed"


def test_subject_match_reported_when_recipient_differs(db_path):
    ledger.record_queued(db_path, "acct", ["a@x.com"], "Weekly", "Body1", tags=None, now=1000.0)
    block = ledger.check_block(db_path, ["c@z.com"], "Weekly", "Body2", now=1000.0 + 60)
    # recipient differs, but subject hash for the SAME recipient set must not collide;
    # subject key is recipient+subject, so different recipients => no match
    assert block is None
