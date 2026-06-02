import base64
import binascii
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from email_mcp import textutil, tlsctx

# Total attachment size cap, matching common provider limits (Gmail/iCloud ~25MB).
MAX_ATTACH_BYTES = 25 * 1024 * 1024


def _split_mime(mime_type, filename):
    """Pick (maintype, subtype): explicit mime_type wins, else guess from the
    filename extension, else the safe generic application/octet-stream."""
    if mime_type and "/" in mime_type:
        maintype, _, subtype = mime_type.partition("/")
        return maintype, subtype
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed and "/" in guessed:
        maintype, _, subtype = guessed.partition("/")
        return maintype, subtype
    return "application", "octet-stream"


def _prepare_attachment(att):
    """Resolve one attachment spec to (data, maintype, subtype, filename). Accepts
    either {'path': local_file} (read from disk) or {'content': base64-string,
    'filename': name}. Raises ValueError on a malformed/missing spec."""
    if not isinstance(att, dict):
        raise ValueError("each attachment must be an object")
    path = att.get("path")
    if path:
        if not os.path.isfile(path):
            raise ValueError(f"attachment file not found: {path}")
        with open(path, "rb") as f:
            data = f.read()
        filename = att.get("filename") or os.path.basename(path)
    elif att.get("content") is not None:
        try:
            data = base64.b64decode(att["content"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("attachment 'content' must be valid base64")
        filename = att.get("filename")
        if not filename:
            raise ValueError("base64 attachment requires a 'filename'")
    else:
        raise ValueError("attachment needs either 'path' or 'content'")
    maintype, subtype = _split_mime(att.get("mime_type"), filename)
    # Sanitize the outgoing filename to a bare component (no separators/control
    # chars) — keeps the Content-Disposition header clean and predictable.
    return data, maintype, subtype, textutil.safe_filename(filename)


def send(acc, to, subject, body, attachments=None):
    msg = EmailMessage()
    msg["From"] = acc["email"]
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    msg.set_content(body)

    if attachments:
        total = 0
        for att in attachments:
            data, maintype, subtype, filename = _prepare_attachment(att)
            total += len(data)
            if total > MAX_ATTACH_BYTES:
                raise ValueError(
                    f"attachments exceed {MAX_ATTACH_BYTES // (1024 * 1024)}MB limit")
            msg.add_attachment(data, maintype=maintype, subtype=subtype,
                               filename=filename)

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
