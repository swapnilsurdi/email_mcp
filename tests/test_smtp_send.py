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
