import os
import threading
from pathlib import Path

from email_mcp import accounts, mcache, security, store

_DEFAULT_DB = Path.home() / ".local/state/email-mcp/state.db"
_DEFAULT_DOWNLOAD_DIR = Path.home() / ".local/state/email-mcp/attachments"
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


def download_dir():
    """Base directory downloaded attachments are written to. Override with
    EMAIL_MCP_DOWNLOAD_DIR. Created on demand. This is the sandbox root — a download
    is always confined to a single file directly inside it (see email_ops)."""
    p = os.environ.get("EMAIL_MCP_DOWNLOAD_DIR", str(_DEFAULT_DOWNLOAD_DIR))
    Path(p).mkdir(parents=True, exist_ok=True)
    return p


def effective_account(name=None):
    af, db = accounts_file(), db_path()
    if name is None:
        name = accounts.resolve_default(af, db)
    return accounts.get_account(af, name)


def security_policy():
    """The security policy from the accounts file's `security:` section. Loaded per
    call (like the accounts themselves) so config edits apply without a restart."""
    return security.load_policy(accounts_file())


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---- in-memory message cache (shared by the prefetch poller + reads) ------------
_MESSAGE_CACHE = None
_MESSAGE_CACHE_LOCK = threading.Lock()


def message_cache():
    """Process-wide MessageCache singleton, sized from env. Bounded by BOTH an entry
    count and a byte budget (whichever trips first), with each cached body trimmed —
    so worst-case RSS stays small regardless of mailbox/attachment sizes.
      EMAIL_MCP_CACHE_ENTRIES   (default 256)   max messages held
      EMAIL_MCP_CACHE_BYTES     (default 32MiB)  max total cached body bytes
      EMAIL_MCP_CACHE_BODY_MAX  (default 64KiB)  per-message cached-body cap
      EMAIL_MCP_CACHE_RECENT_TTL(default 180s)   how long a 'latest' view stays fresh
    """
    global _MESSAGE_CACHE
    if _MESSAGE_CACHE is not None:
        return _MESSAGE_CACHE
    with _MESSAGE_CACHE_LOCK:               # double-checked: avoid two caches racing
        if _MESSAGE_CACHE is None:
            _MESSAGE_CACHE = mcache.MessageCache(
                max_entries=_env_int("EMAIL_MCP_CACHE_ENTRIES", 256),
                max_bytes=_env_int("EMAIL_MCP_CACHE_BYTES", 32 * 1024 * 1024),
                body_max=_env_int("EMAIL_MCP_CACHE_BODY_MAX", 64 * 1024),
                recent_ttl=_env_int("EMAIL_MCP_CACHE_RECENT_TTL", 180),
            )
    return _MESSAGE_CACHE


def prefetch_config():
    """Background prefetch knobs. Interval 0 = OFF (the default — kept off so the
    watchdog's short-lived `claude -p` runs never start a poller; the interactive
    registration enables it via EMAIL_MCP_PREFETCH_INTERVAL=120)."""
    return {
        "interval": _env_int("EMAIL_MCP_PREFETCH_INTERVAL", 0),
        "count": _env_int("EMAIL_MCP_PREFETCH_COUNT", 50),
        "folder": os.environ.get("EMAIL_MCP_PREFETCH_FOLDER", "INBOX"),
    }
