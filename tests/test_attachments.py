import email.message
import os

import pytest

from email_mcp import email_ops, textutil


def _msg(*atts, body="hi"):
    m = email.message.EmailMessage()
    m["Message-ID"] = "<m@x>"
    m.set_content(body)
    for filename, mime, data in atts:
        maintype, _, subtype = mime.partition("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return email.message_from_bytes(bytes(m))


ACC = {"name": "a", "email": "me@x.com"}


def _fetch(msg):
    def fetch_fn(acc, message_id, folders, uid=None, folder=None):
        return ("INBOX", msg)
    return fetch_fn


def test_download_single_attachment(tmp_path):
    msg = _msg(("report.pdf", "application/pdf", b"%PDF data"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        fetch_fn=_fetch(msg))
    assert res["status"] == "downloaded"
    assert res["filename"] == "report.pdf"
    assert res["size"] == len(b"%PDF data")
    assert os.path.dirname(os.path.realpath(res["saved_path"])) == \
        os.path.realpath(str(tmp_path))
    with open(res["saved_path"], "rb") as f:
        assert f.read() == b"%PDF data"


def test_download_selects_by_index_and_filename(tmp_path):
    msg = _msg(("a.txt", "text/plain", b"alpha"), ("b.txt", "text/plain", b"bravo"))
    r_idx = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), index=1,
        fetch_fn=_fetch(msg))
    assert r_idx["filename"] == "b.txt"
    r_name = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), filename="a.txt",
        fetch_fn=_fetch(msg))
    assert open(r_name["saved_path"], "rb").read() == b"alpha"


def test_download_ambiguous_returns_available(tmp_path):
    msg = _msg(("a.txt", "text/plain", b"x"), ("b.txt", "text/plain", b"y"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    assert res["error"] == "attachment_not_selected"
    assert [a["filename"] for a in res["available"]] == ["a.txt", "b.txt"]


def test_download_not_found(tmp_path):
    def miss(acc, mid, folders, uid=None, folder=None):
        return (None, None)
    res = email_ops.download_attachment(
        ACC, "<gone@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=miss)
    assert res["error"] == "not_found"


def test_download_no_attachments(tmp_path):
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        fetch_fn=_fetch(_msg(body="text only")))
    assert res["error"] == "no_attachments"


def test_download_malicious_filename_confined(tmp_path):
    # An attacker-supplied traversal filename must land INSIDE dest_dir, not above it.
    msg = _msg(("../../../../etc/passwd", "text/plain", b"pwned"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    assert res["status"] == "downloaded"
    real = os.path.realpath(res["saved_path"])
    assert os.path.dirname(real) == os.path.realpath(str(tmp_path))
    assert res["filename"] == "passwd"
    # nothing was written outside the sandbox
    assert not os.path.exists(os.path.realpath(str(tmp_path / ".." / ".." / "etc" / "passwd")))


def test_download_dedupes_without_overwrite(tmp_path):
    msg = _msg(("f.txt", "text/plain", b"one"))
    r1 = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    r2 = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    assert r1["saved_path"] != r2["saved_path"]
    assert "(1)" in r2["filename"]


def test_download_overwrite_reuses_path(tmp_path):
    msg = _msg(("f.txt", "text/plain", b"one"))
    r1 = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    r2 = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), overwrite=True,
        fetch_fn=_fetch(msg))
    assert r1["saved_path"] == r2["saved_path"]


def test_safe_dest_guard_rejects_escape(tmp_path, monkeypatch):
    # Defense-in-depth: even if sanitizing were bypassed, the realpath containment
    # check must refuse a name that escapes the base directory.
    monkeypatch.setattr(textutil, "safe_filename", lambda n, fallback="attachment": "../escape")
    with pytest.raises(ValueError, match="escapes"):
        email_ops._safe_dest(str(tmp_path), "anything", overwrite=False)


def test_download_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(email_ops, "MAX_DOWNLOAD_BYTES", 4)
    msg = _msg(("big.bin", "application/octet-stream", b"way too big"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path), fetch_fn=_fetch(msg))
    assert res["error"] == "attachment_too_large"
    assert res["limit"] == 4 and res["size"] == len(b"way too big")
    # nothing written
    assert not any(p.is_file() for p in tmp_path.iterdir())


def test_download_all_writes_every_attachment(tmp_path):
    msg = _msg(("a.txt", "text/plain", b"alpha"), ("b.txt", "text/plain", b"bravo"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        download_all=True, fetch_fn=_fetch(msg))
    assert res["status"] == "downloaded_all" and res["count"] == 2
    names = sorted(os.path.basename(r["saved_path"]) for r in res["attachments"])
    assert names == ["a.txt", "b.txt"]


def test_download_all_count_excludes_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(email_ops, "MAX_INLINE_BYTES", 4)
    msg = _msg(("ok.txt", "text/plain", b"hi"),
               ("big.bin", "application/octet-stream", b"too big"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        download_all=True, return_base64=True, fetch_fn=_fetch(msg))
    assert res["count"] == 1                      # only ok.txt inlined; big.bin errored
    statuses = [a.get("status", a.get("error")) for a in res["attachments"]]
    assert "inline" in statuses and "attachment_too_large_for_inline" in statuses


def test_return_base64_inline_no_disk(tmp_path):
    import base64
    msg = _msg(("d.csv", "text/csv", b"x,y\n1,2\n"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        return_base64=True, fetch_fn=_fetch(msg))
    assert res["status"] == "inline"
    assert base64.b64decode(res["content_base64"]) == b"x,y\n1,2\n"
    assert "saved_path" not in res
    assert not any(tmp_path.iterdir())          # nothing written to disk


def test_return_base64_rejects_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(email_ops, "MAX_INLINE_BYTES", 4)
    msg = _msg(("d.bin", "application/octet-stream", b"too big data"))
    res = email_ops.download_attachment(
        ACC, "<m@x>", folders=["INBOX"], dest_dir=str(tmp_path),
        return_base64=True, fetch_fn=_fetch(msg))
    assert res["error"] == "attachment_too_large_for_inline" and res["limit"] == 4


def test_uid_folder_passed_to_fetch(tmp_path):
    seen = {}

    def fetch_fn(acc, message_id, folders, uid=None, folder=None):
        seen["uid"], seen["folder"] = uid, folder
        return ("INBOX", _msg(("a.txt", "text/plain", b"x")))
    email_ops.download_attachment(
        ACC, "", folders=["INBOX"], dest_dir=str(tmp_path), uid=99, folder="INBOX",
        fetch_fn=fetch_fn)
    assert seen["uid"] == 99 and seen["folder"] == "INBOX"


def test_send_email_passes_attachments_through(db_path):
    seen = {}

    def fake_send(acc, to, subject, body, attachments=None):
        seen["attachments"] = attachments
        return "<mid@x>"

    acc = {"name": "a", "email": "me@x.com"}
    res = email_ops.send_email(
        db_path, acc, to=["x@y.com"], subject="S", body="B", tags=None,
        attachments=[{"path": "/tmp/x.pdf"}], send_fn=fake_send, now=1.0)
    assert res["status"] == "sent"
    assert seen["attachments"] == [{"path": "/tmp/x.pdf"}]
