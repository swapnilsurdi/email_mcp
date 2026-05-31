from email_mcp.providers import imap_account


class FakeIMAP:
    def __init__(self):
        self.selected = None
        self.readonly = None
        self.logged_in = False
        self.store_calls = []

    def login(self, user, pw):
        self.logged_in = True
        return ("OK", [b"ok"])

    def list(self, *a, **k):
        return ("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren) "/" "Job"',
            b'(\\HasNoChildren) "/" "Job/Job Applications"',
            b'(\\HasNoChildren) "/" "Sent Messages"',
        ])

    def select(self, folder, readonly=False):
        self.selected = folder
        self.readonly = readonly
        return ("OK", [b"1"])

    def close(self):
        pass

    def logout(self):
        pass


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "imap_host": "h", "imap_port": 993}


def test_list_folders_parses_names():
    fake = FakeIMAP()
    folders = imap_account.list_folders(ACC, connect_fn=lambda acc: fake)
    assert "INBOX" in folders
    assert "Job/Job Applications" in folders
    assert "Sent Messages" in folders


import email as email_lib
import email.message  # ensure email.message submodule is importable on Python 3.13


def _raw_email(subject="Hi", frm="s@x.com", to="me@x.com", body="hello body"):
    m = email_lib.message.EmailMessage()
    m["Subject"] = subject
    m["From"] = frm
    m["To"] = to
    m["Message-ID"] = f"<{subject}@x>"
    m["Date"] = "Wed, 28 May 2026 10:00:00 +0000"
    m.set_content(body)
    return bytes(m)


class FakeIMAPWithMsgs(FakeIMAP):
    def __init__(self):
        super().__init__()
        self.fetch_specs = []

    def search(self, charset, *criteria):
        return ("OK", [b"1 2"])

    def fetch(self, message_set, spec):
        # batched fetch: message_set like "1,2" -> one tuple per message
        self.fetch_specs.append(spec)
        resp = []
        for n in message_set.replace(",", " ").split():
            resp.append((f"{n} (BODY[])".encode(), _raw_email(subject=f"Msg{n}")))
            resp.append(b")")
        return ("OK", resp)


def test_fetch_uses_peek_and_readonly():
    fake = FakeIMAPWithMsgs()
    msgs = imap_account.fetch_folder(
        ACC, "INBOX", criteria=["SINCE", "01-Jan-2026"],
        connect_fn=lambda acc: fake)
    assert fake.readonly is True
    assert all("PEEK" in s for s in fake.fetch_specs)
    assert len(msgs) == 2
    assert msgs[0]["subject"].startswith("Msg")
    assert msgs[0]["body"] == "hello body\n"
    assert "message_id" in msgs[0] and "from_address" in msgs[0]


class FakeIMAPMutating(FakeIMAPWithMsgs):
    def __init__(self):
        super().__init__()
        self.uid_calls = []

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command == "SEARCH":
            return ("OK", [b"7"])     # found UID 7
        return ("OK", [b"done"])


def test_mark_read_sets_seen_flag():
    fake = FakeIMAPMutating()
    res = imap_account.mark_message(
        ACC, "<Msg@x>", read=True, folders=["INBOX"], connect_fn=lambda acc: fake)
    assert res["read"] is True
    store_calls = [c for c in fake.uid_calls if c[0] == "STORE"]
    assert store_calls and "+FLAGS" in store_calls[0][1]
    assert "\\Seen" in store_calls[0][1][-1]


def test_move_uses_uid_move_or_copy():
    fake = FakeIMAPMutating()
    res = imap_account.move_message(
        ACC, "<Msg@x>", dest_folder="Archive", folders=["INBOX"],
        connect_fn=lambda acc: fake)
    assert res["dest_folder"] == "Archive"
    cmds = [c[0] for c in fake.uid_calls]
    assert "MOVE" in cmds or "COPY" in cmds


import imaplib


class FakeIMAPStrictNames(FakeIMAPMutating):
    """Mimics a real server: an unquoted mailbox name containing a space is a
    syntax error (the 'EXAMINE ... BAD Parse Error' we hit against iCloud)."""

    @staticmethod
    def _check(name):
        if " " in name and not (name.startswith('"') and name.endswith('"')):
            raise imaplib.IMAP4.error(
                f"EXAMINE command error: BAD [b'Parse Error'] for {name!r}")

    def select(self, folder, readonly=False):
        self._check(folder)
        return super().select(folder, readonly=readonly)

    def uid(self, command, *args):
        # MOVE/COPY destination is the last arg
        if command in ("MOVE", "COPY") and args:
            self._check(args[-1])
        return super().uid(command, *args)


def test_fetch_quotes_folder_name_with_spaces():
    fake = FakeIMAPStrictNames()
    msgs = imap_account.fetch_folder(
        ACC, "Deleted Messages", criteria=["ALL"], connect_fn=lambda acc: fake)
    assert len(msgs) == 2  # did not raise => folder name was quoted


def test_move_quotes_dest_folder_with_spaces():
    fake = FakeIMAPStrictNames()
    res = imap_account.move_message(
        ACC, "<Msg@x>", dest_folder="Deleted Messages", folders=["INBOX"],
        connect_fn=lambda acc: fake)
    assert res["dest_folder"] == "Deleted Messages"  # did not raise => quoted


def test_encode_folder_modified_utf7_non_ascii():
    # "é" (U+00E9) -> UTF-16BE 00 E9 -> base64 'AOk' -> &AOk- ; whole name quoted
    assert imap_account._encode_folder("Café") == '"Caf&AOk-"'


def test_encode_folder_escapes_literal_ampersand():
    # literal '&' must become '&-' in modified UTF-7
    assert imap_account._encode_folder("A&B") == '"A&-B"'


def test_encode_folder_ascii_unchanged_but_quoted():
    assert imap_account._encode_folder("Deleted Messages") == '"Deleted Messages"'


def test_parse_folder_name_decodes_modified_utf7():
    line = b'(\\HasNoChildren) "/" "Caf&AOk-"'
    assert imap_account._parse_folder_name(line) == "Café"


def test_folder_name_roundtrips():
    for name in ("INBOX", "Deleted Messages", "Café", "A&B", "日本"):
        line = ('(\\HasNoChildren) "/" ' + imap_account._encode_folder(name)).encode()
        assert imap_account._parse_folder_name(line) == name
