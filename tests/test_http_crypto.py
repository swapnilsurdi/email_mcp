import pytest

from email_mcp.http import crypto


def test_round_trip_with_explicit_key():
    salt, token = crypto.encrypt_secret("hunter2", master_key="master-abc")
    assert crypto.decrypt_secret(salt, token, master_key="master-abc") == "hunter2"


def test_same_plaintext_encrypts_differently():
    a = crypto.encrypt_secret("pw", master_key="k")
    b = crypto.encrypt_secret("pw", master_key="k")
    assert a != b                       # fresh salt + nonce each time


def test_wrong_key_fails():
    salt, token = crypto.encrypt_secret("pw", master_key="right")
    with pytest.raises(Exception):
        crypto.decrypt_secret(salt, token, master_key="wrong")


def test_missing_master_key_refuses(monkeypatch):
    monkeypatch.delenv("EMAIL_MCP_MASTER_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.encrypt_secret("pw")
    assert crypto.master_key_configured() is False
    assert crypto.master_key_configured(master_key="x") is True


def test_env_master_key_used(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_MASTER_KEY", "from-env")
    salt, token = crypto.encrypt_secret("pw")
    assert crypto.decrypt_secret(salt, token) == "pw"
