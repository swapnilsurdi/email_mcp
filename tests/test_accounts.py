import pytest
from email_mcp import accounts

ACCOUNTS_YML = """
accounts:
  - name: icloud-personal
    email: me@icloud.com
    imap_host: imap.mail.me.com
    imap_port: 993
    smtp_host: smtp.mail.me.com
    smtp_port: 587
    default: true
  - name: gmail-personal
    email: me@gmail.com
    imap_host: imap.gmail.com
    imap_port: 993
    smtp_host: smtp.gmail.com
    smtp_port: 587
"""


@pytest.fixture
def accounts_file(tmp_path):
    p = tmp_path / "accounts.yml"
    p.write_text(ACCOUNTS_YML)
    return str(p)


def test_list_accounts_reads_yaml(accounts_file):
    accs = accounts.load_accounts(accounts_file)
    names = [a["name"] for a in accs]
    assert names == ["icloud-personal", "gmail-personal"]


def test_yaml_default_used_when_no_db_default(accounts_file, db_path):
    name = accounts.resolve_default(accounts_file, db_path)
    assert name == "icloud-personal"


def test_db_default_overrides_yaml(accounts_file, db_path):
    accounts.set_default(accounts_file, db_path, "gmail-personal")
    assert accounts.resolve_default(accounts_file, db_path) == "gmail-personal"


def test_set_default_rejects_unknown(accounts_file, db_path):
    with pytest.raises(ValueError):
        accounts.set_default(accounts_file, db_path, "nope")


def test_get_account_resolves_password_from_keychain(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password",
                        lambda service, user: "app-pw" if user == "icloud-personal" else None)
    acc = accounts.get_account(accounts_file, "icloud-personal")
    assert acc["password"] == "app-pw"
    assert acc["imap_host"] == "imap.mail.me.com"


def test_get_account_missing_password_raises(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password", lambda s, u: None)
    with pytest.raises(RuntimeError, match="No password for account"):
        accounts.get_account(accounts_file, "icloud-personal")


def test_get_account_uses_generic_env_password(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password", lambda s, u: None)
    monkeypatch.setenv("EMAIL_MCP_PASSWORD", "env-pw")
    acc = accounts.get_account(accounts_file, "icloud-personal")
    assert acc["password"] == "env-pw"


def test_per_account_env_password_overrides_generic(accounts_file, monkeypatch):
    monkeypatch.setattr(accounts.keyring, "get_password", lambda s, u: None)
    monkeypatch.setenv("EMAIL_MCP_PASSWORD", "generic")
    monkeypatch.setenv("EMAIL_MCP_PASSWORD_ICLOUD_PERSONAL", "specific")
    acc = accounts.get_account(accounts_file, "icloud-personal")
    assert acc["password"] == "specific"
