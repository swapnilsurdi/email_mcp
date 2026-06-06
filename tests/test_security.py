from email_mcp import email_ops, security
from email_mcp.providers import imap_account


# ---- recipient allowlist ----------------------------------------------------------

def test_no_allowlist_allows_everyone():
    p = security.SecurityPolicy()
    assert p.denied_recipients(["anyone@anywhere.com"]) == []


def test_plain_address_and_regex_entries():
    p = security.SecurityPolicy(allowed_recipients=[
        "me@icloud\\.com", ".*@surdi\\.in"])
    assert p.denied_recipients(["me@icloud.com"]) == []
    assert p.denied_recipients(["ME@ICLOUD.COM"]) == []          # case-insensitive
    assert p.denied_recipients(["cherry@surdi.in"]) == []
    assert p.denied_recipients(["evil@attacker.com"]) == ["evil@attacker.com"]
    # FULL match: a pattern can't match as a substring of a longer address
    assert p.denied_recipients(["me@icloud.com.evil.io"]) == ["me@icloud.com.evil.io"]


def test_empty_allowlist_blocks_all():
    p = security.SecurityPolicy(allowed_recipients=[])
    assert p.denied_recipients(["me@icloud.com"]) == ["me@icloud.com"]


# ---- three-tier classification (blocklist / allowlist / approve) -------------------

def test_classify_three_tiers():
    p = security.SecurityPolicy(allowed_recipients=["ok@x\\.com"],
                                blocked_recipients=["bad@x\\.com"])
    assert p.classify_recipient("ok@x.com") == "allow"
    assert p.classify_recipient("bad@x.com") == "block"
    assert p.classify_recipient("new@y.com") == "approve"
    assert p.classify_recipients(["ok@x.com", "bad@x.com", "new@y.com"]) == {
        "ok@x.com": "allow", "bad@x.com": "block", "new@y.com": "approve"}


def test_blocklist_wins_over_allowlist_and_no_allowlist():
    p = security.SecurityPolicy(allowed_recipients=["bad@x\\.com"],
                                blocked_recipients=["bad@x\\.com"])
    assert p.classify_recipient("bad@x.com") == "block"
    # the blocklist applies even when there is no allowlist at all
    p2 = security.SecurityPolicy(blocked_recipients=["bad@x\\.com"])
    assert p2.classify_recipient("bad@x.com") == "block"
    assert p2.classify_recipient("anyone@y.com") == "allow"
    assert p2.denied_recipients(["bad@x.com", "anyone@y.com"]) == ["bad@x.com"]


def test_no_allowlist_no_blocklist_classifies_allow():
    p = security.SecurityPolicy()
    assert p.classify_recipient("anyone@anywhere.com") == "allow"


def test_multi_address_element_is_expanded_before_matching():
    # One list element bundling a second address behind a comma must be split and
    # validated address-by-address — not matched as one opaque string. With a broad
    # allowlist the bundled address would otherwise slip through.
    p = security.SecurityPolicy(allowed_recipients=[".*@surdi\\.in"])
    assert p.denied_recipients(["me@surdi.in, other@untrusted_domain.com"]) \
        == ["other@untrusted_domain.com"]
    # an all-allowed bundle passes
    assert p.denied_recipients(["me@surdi.in, you@surdi.in"]) == []


def test_display_name_element_is_reduced_to_bare_address():
    p = security.SecurityPolicy(allowed_recipients=[".*@surdi\\.in"])
    assert p.denied_recipients(["Cherry <cherry@surdi.in>"]) == []
    assert p.denied_recipients(["Bob <bob@untrusted_domain.com>"]) \
        == ["bob@untrusted_domain.com"]


def test_expand_recipients_helper():
    assert security.expand_recipients(["a@x.com, b@y.com", "c@z.com"]) \
        == ["a@x.com", "b@y.com", "c@z.com"]
    assert security.expand_recipients(["Name <n@x.com>"]) == ["n@x.com"]


# ---- built-in trash protection ----------------------------------------------------

def test_trash_names_protected_across_providers():
    p = security.SecurityPolicy()
    for f in ("Trash", "trash", "Bin", "[Gmail]/Trash", "[Gmail]/Bin",
              "Deleted Messages", "Deleted Items", "Trash/2019",
              "[Gmail]/Trash/old"):
        assert p.folder_protected(f), f


def test_lookalike_folders_not_protected():
    p = security.SecurityPolicy()
    for f in ("Binary", "Robin", "Binders", "Trashy", "INBOX", "Deleted",
              "DeletedStuff", "Archive"):
        assert not p.folder_protected(f), f


def test_protect_trash_opt_out():
    p = security.SecurityPolicy(protect_trash=False)
    assert not p.folder_protected("Trash")
    assert not p.folder_protected("Deleted Messages")


def test_user_protected_folders_fullmatch_regex():
    p = security.SecurityPolicy(protected_folders=["Archive(/.*)?", "Job"])
    assert p.folder_protected("Archive")
    assert p.folder_protected("Archive/2024")
    assert p.folder_protected("job")          # case-insensitive
    assert not p.folder_protected("Jobs")     # full match, not prefix


# ---- read access ------------------------------------------------------------------

def test_blocked_wins_over_readable():
    p = security.SecurityPolicy(readable_folders=[".*"],
                                blocked_folders=["Private(/.*)?"])
    assert p.folder_readable("INBOX")
    assert not p.folder_readable("Private")
    assert not p.folder_readable("Private/keys")


def test_readable_allowlist_restricts():
    p = security.SecurityPolicy(readable_folders=["INBOX", "Receipts(/.*)?"])
    assert p.folder_readable("INBOX")
    assert p.folder_readable("Receipts/2026")
    assert not p.folder_readable("Sent Messages")
    assert p.filter_readable(["INBOX", "Sent Messages"]) == ["INBOX"]


def test_protected_folder_still_readable():
    p = security.SecurityPolicy()
    assert p.folder_readable("Trash")       # read-only, but readable


# ---- yaml loader ------------------------------------------------------------------

def test_load_policy_from_yaml(tmp_path):
    f = tmp_path / "accounts.yml"
    f.write_text(
        "security:\n"
        "  allowed_recipients:\n    - me@x\\.com\n"
        "  protected_folders:\n    - Keep\n"
        "  blocked_folders:\n    - Secret\n"
        "  protect_trash: false\n"
        "accounts:\n  - name: a\n    email: me@x.com\n")
    p = security.load_policy(str(f))
    assert p.denied_recipients(["other@x.com"]) == ["other@x.com"]
    assert p.folder_protected("Keep") and not p.folder_protected("Trash")
    assert not p.folder_readable("Secret")


def test_load_policy_missing_file_is_permissive_but_trash_protected(tmp_path):
    p = security.load_policy(str(tmp_path / "nope.yml"))
    assert p.denied_recipients(["x@y.com"]) == []
    assert p.folder_protected("Trash")
    assert p.folder_readable("Anything")


# ---- enforcement: send ------------------------------------------------------------

ACC = {"name": "a", "email": "me@x.com"}


def test_send_blocked_recipient_never_sent_or_recorded(db_path):
    sent = []

    def fake(acc, to, subject, body, attachments=None):
        sent.append(to)
        return "<m@x>"
    p = security.SecurityPolicy(allowed_recipients=["ok@x\\.com"])
    r = email_ops.send_email(db_path, ACC, to=["ok@x.com", "evil@y.com"],
                             subject="S", body="B", tags=None, policy=p,
                             send_fn=fake, now=1000.0)
    assert r["status"] == "BLOCKED" and r["reason"] == "recipient_not_allowed"
    assert r["denied_recipients"] == ["evil@y.com"]
    assert sent == []                                   # never hit SMTP
    # nothing recorded -> an immediately following allowed send is NOT dedup-blocked
    r2 = email_ops.send_email(db_path, ACC, to=["ok@x.com"], subject="S", body="B",
                              tags=None, policy=p, send_fn=fake, now=1001.0)
    assert r2["status"] == "sent"


def test_send_comma_smuggled_recipient_blocked(db_path):
    sent = []

    def fake(acc, to, subject, body, attachments=None):
        sent.append(to)
        return "<m@x>"
    p = security.SecurityPolicy(allowed_recipients=[".*"])   # deliberately broad
    r = email_ops.send_email(
        db_path, ACC, to=["ok@x.com, other@y.com"], subject="S", body="B",
        tags=None, policy=p, send_fn=fake, now=1000.0)
    # Even a permissive `.*` allows each address individually, so this bundle is NOT
    # blocked by the allowlist — but it MUST reach SMTP as two canonical addresses,
    # never as the single un-split smuggle string.
    assert r["status"] == "sent"
    assert sent == [["ok@x.com", "other@y.com"]]


def test_send_canonicalizes_recipients_passed_to_smtp(db_path):
    sent = []

    def fake(acc, to, subject, body, attachments=None):
        sent.append(to)
        return "<m@x>"
    r = email_ops.send_email(
        db_path, ACC, to=["Cherry <c@surdi.in>", "a@x.com, b@x.com"],
        subject="S", body="B", tags=None, send_fn=fake, now=1000.0)
    assert r["status"] == "sent"
    assert sent == [["c@surdi.in", "a@x.com", "b@x.com"]]


def test_send_no_valid_recipient_blocked(db_path):
    def fake(acc, to, subject, body, attachments=None):
        raise AssertionError("must not reach SMTP with no recipient")
    for empty in ([], [""], ["   "]):
        r = email_ops.send_email(db_path, ACC, to=empty, subject="S",
                                 body="B", tags=None, send_fn=fake, now=1000.0)
        assert r["status"] == "BLOCKED" and r["reason"] == "no_valid_recipients", empty


# ---- enforcement: move/mark -------------------------------------------------------

class FakeIMAPLocate:
    """Locates any message in the FIRST folder searched; records mutations."""

    def __init__(self):
        self.mutations = []

    def login(self, *a):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self._current = folder
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command == "SEARCH":
            return ("OK", [b"7"])
        self.mutations.append((command, args))
        return ("OK", [b"done"])

    def expunge(self):
        self.mutations.append(("EXPUNGE", ()))
        return ("OK", [b""])

    def logout(self):
        pass


def test_move_into_trash_blocked_before_connecting():
    def no_connect(acc):
        raise AssertionError("must not even connect for a protected destination")
    p = security.SecurityPolicy()
    r = imap_account.move_message(ACC, "<m@x>", dest_folder="Deleted Messages",
                                  folders=["INBOX"], policy=p, connect_fn=no_connect)
    assert r["error"] == "folder_protected" and r["folder"] == "Deleted Messages"


def test_move_out_of_protected_source_blocked():
    fake = FakeIMAPLocate()
    p = security.SecurityPolicy(protected_folders=["Keep"])
    r = imap_account.move_message(ACC, "<m@x>", dest_folder="Archive",
                                  folders=["Keep"], policy=p,
                                  connect_fn=lambda acc: fake)
    assert r["error"] == "folder_protected" and r["folder"] == "Keep"
    assert fake.mutations == []                          # no MOVE/COPY/EXPUNGE issued


def test_move_into_blocked_folder_refused():
    def no_connect(acc):
        raise AssertionError("must not connect for a blocked destination")
    p = security.SecurityPolicy(blocked_folders=["Secret"])
    r = imap_account.move_message(ACC, "<m@x>", dest_folder="Secret",
                                  folders=["INBOX"], policy=p, connect_fn=no_connect)
    assert r["error"] == "folder_blocked" and r["folder"] == "Secret"


def test_move_between_normal_folders_still_works():
    fake = FakeIMAPLocate()
    p = security.SecurityPolicy()
    r = imap_account.move_message(ACC, "<m@x>", dest_folder="Archive",
                                  folders=["INBOX"], policy=p,
                                  connect_fn=lambda acc: fake)
    assert r.get("dest_folder") == "Archive"
    assert any(c[0] in ("MOVE", "COPY") for c in fake.mutations)


def test_mark_in_protected_folder_blocked():
    fake = FakeIMAPLocate()
    p = security.SecurityPolicy()
    r = imap_account.mark_message(ACC, "<m@x>", read=True, folders=["Trash"],
                                  policy=p, connect_fn=lambda acc: fake)
    assert r["error"] == "folder_protected"
    assert fake.mutations == []                          # no STORE issued


# ---- enforcement: reads -----------------------------------------------------------

def test_get_emails_filters_blocked_folders(db_path):
    seen = []

    def fake_fetch(acc, folder, criteria, limit=None, connect_fn=None):
        seen.append(folder)
        return []
    p = security.SecurityPolicy(blocked_folders=["Secret"])
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["INBOX", "Secret"],
        include_sent=False, strip_to_text=False, page=1, page_size=5,
        policy=p, fetch_fn=fake_fetch,
        folders_fn=lambda a, connect_fn=None: ["INBOX", "Secret"], now=1000.0)
    assert seen == ["INBOX"]                            # Secret never touched
    assert out["folders_denied"] == ["Secret"]


def test_get_emails_all_denied_is_an_error(db_path):
    p = security.SecurityPolicy(blocked_folders=["Secret"])
    out = email_ops.get_emails(
        db_path, ACC, filters=None, query=None, folders=["Secret"],
        include_sent=False, strip_to_text=False, page=1, page_size=5,
        policy=p, fetch_fn=lambda *a, **k: [],
        folders_fn=lambda a, connect_fn=None: ["Secret"], now=1000.0)
    assert out["error"] == "folders_blocked"


def test_download_blocked_explicit_folder(tmp_path):
    p = security.SecurityPolicy(blocked_folders=["Secret"])
    r = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        uid=7, folder="Secret", policy=p,
        fetch_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert r["error"] == "folder_blocked"
