import os
from pathlib import Path

from email_mcp import accounts, store

_DEFAULT_DB = Path.home() / ".local/state/email-mcp/state.db"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Default to a user config dir so the installed package works without a source
# checkout. Override with EMAIL_MCP_ACCOUNTS. (A repo-local config/accounts.yml is
# only used when explicitly pointed at via that env var.)
_DEFAULT_ACCOUNTS = Path.home() / ".config/email-mcp/accounts.yml"


def load_dotenv(path=None):
    """Dev convenience: load KEY=VALUE lines from a .env file into the environment
    WITHOUT overriding values already set (os.environ wins). Called from the server
    entrypoint only, so importing the package in tests never reads .env. Secrets
    loaded here are never logged or returned by any tool."""
    path = Path(path) if path else (_PROJECT_ROOT / ".env")
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def db_path():
    p = os.environ.get("EMAIL_MCP_DB", str(_DEFAULT_DB))
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    store.init_db(p)
    return p


def accounts_file():
    return os.environ.get("EMAIL_MCP_ACCOUNTS", str(_DEFAULT_ACCOUNTS))


def effective_account(name=None):
    af, db = accounts_file(), db_path()
    if name is None:
        name = accounts.resolve_default(af, db)
    return accounts.get_account(af, name)
