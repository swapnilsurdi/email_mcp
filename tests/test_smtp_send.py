from email_mcp.providers import smtp_send


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = False
        self.sent = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, pw):
        self.logged_in = True

    def send_message(self, msg):
        self.sent = msg

    def quit(self):
        pass


ACC = {"name": "a", "email": "me@x.com", "password": "pw",
       "smtp_host": "smtp.h", "smtp_port": 587}


def test_send_uses_starttls_and_login(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    mid = smtp_send.send(ACC, to=["x@y.com"], subject="Hi", body="Body")
    inst = FakeSMTP.instances[0]
    assert inst.started_tls is True
    assert inst.logged_in is True
    assert inst.sent["To"] == "x@y.com"
    assert inst.sent["From"] == "me@x.com"
    assert mid  # a Message-ID string is returned


import base64
import pytest


def _attachments_of(msg):
    return [p for p in msg.walk()
            if str(p.get("Content-Disposition", "")).startswith("attachment")]


def test_send_with_path_attachment(monkeypatch, tmp_path):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 hello")
    smtp_send.send(ACC, to=["x@y.com"], subject="Hi", body="Body",
                   attachments=[{"path": str(f)}])
    atts = _attachments_of(FakeSMTP.instances[0].sent)
    assert len(atts) == 1
    assert atts[0].get_filename() == "report.pdf"
    assert atts[0].get_payload(decode=True) == b"%PDF-1.4 hello"


def test_send_with_base64_attachment(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    content = base64.b64encode(b"col1,col2\n1,2\n").decode()
    smtp_send.send(ACC, to=["x@y.com"], subject="Hi", body="Body",
                   attachments=[{"content": content, "filename": "data.csv",
                                 "mime_type": "text/csv"}])
    atts = _attachments_of(FakeSMTP.instances[0].sent)
    assert atts[0].get_filename() == "data.csv"
    assert atts[0].get_content_type() == "text/csv"
    assert atts[0].get_payload(decode=True) == b"col1,col2\n1,2\n"


def test_send_base64_requires_filename(monkeypatch):
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    with pytest.raises(ValueError, match="filename"):
        smtp_send.send(ACC, to=["x@y.com"], subject="S", body="B",
                       attachments=[{"content": base64.b64encode(b"x").decode()}])


def test_send_missing_path_raises(monkeypatch):
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    with pytest.raises(ValueError, match="not found"):
        smtp_send.send(ACC, to=["x@y.com"], subject="S", body="B",
                       attachments=[{"path": "/no/such/file.bin"}])


def test_send_rejects_oversized_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_send, "MAX_ATTACH_BYTES", 10)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 50)
    with pytest.raises(ValueError, match="exceed"):
        smtp_send.send(ACC, to=["x@y.com"], subject="S", body="B",
                       attachments=[{"path": str(big)}])


def test_send_outgoing_filename_is_sanitized(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtp_send.smtplib, "SMTP", FakeSMTP)
    content = base64.b64encode(b"x").decode()
    smtp_send.send(ACC, to=["x@y.com"], subject="S", body="B",
                   attachments=[{"content": content, "filename": "../../evil.sh"}])
    assert _attachments_of(FakeSMTP.instances[0].sent)[0].get_filename() == "evil.sh"
