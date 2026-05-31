import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from email_mcp import tlsctx


def send(acc, to, subject, body):
    msg = EmailMessage()
    msg["From"] = acc["email"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    msg.set_content(body)

    context = tlsctx.ssl_context()

    server = smtplib.SMTP(acc["smtp_host"], acc.get("smtp_port", 587), timeout=30)
    try:
        server.ehlo()
        server.starttls(context=context)   # requireTLS: we always STARTTLS
        server.ehlo()
        server.login(acc["email"], acc["password"])
        server.send_message(msg)
        return message_id
    finally:
        try:
            server.quit()
        except Exception:
            pass
