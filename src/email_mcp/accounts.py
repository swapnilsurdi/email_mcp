import os

import keyring
import yaml

from email_mcp import store

KEYRING_SERVICE = "email-mcp"


def _env_password(name):
    """Dev override: read the app password from the environment (e.g. a .env file)
    instead of the Keychain. Per-account `EMAIL_MCP_PASSWORD_<NAME>` wins, then the
    generic `EMAIL_MCP_PASSWORD`. Intended for local development only."""
    per_account = "EMAIL_MCP_PASSWORD_" + name.upper().replace("-", "_")
    return os.environ.get(per_account) or os.environ.get("EMAIL_MCP_PASSWORD")


def load_accounts(accounts_file):
    with open(accounts_file) as f:
        data = yaml.safe_load(f) or {}
    return data.get("accounts", [])


def _find(accounts_file, name):
    for a in load_accounts(accounts_file):
        if a["name"] == name:
            return a
    return None


def resolve_default(accounts_file, db_path):
    db_default = store.get_setting(db_path, "default_account")
    accs = load_accounts(accounts_file)
    valid = {a["name"] for a in accs}
    if db_default in valid:
        return db_default
    for a in accs:
        if a.get("default"):
            return a["name"]
    return accs[0]["name"] if accs else None


def set_default(accounts_file, db_path, name):
    if _find(accounts_file, name) is None:
        raise ValueError(f"Unknown account: {name}")
    store.set_setting(db_path, "default_account", name)


def get_account(accounts_file, name):
    acc = _find(accounts_file, name)
    if acc is None:
        raise ValueError(f"Unknown account: {name}")
    pw = _env_password(name) or keyring.get_password(KEYRING_SERVICE, name)
    if not pw:
        raise RuntimeError(f"No password for account '{name}'. Set it in the Keychain "
                           f"(python -m email_mcp.setup_cli {name}) or, for dev, the "
                           f"EMAIL_MCP_PASSWORD env var / .env file.")
    return {**acc, "password": pw}
