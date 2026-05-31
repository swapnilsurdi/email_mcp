import pytest

from email_mcp import store


@pytest.fixture
def db_path(tmp_path):
    """A fresh, schema-initialized SQLite DB for each test."""
    path = tmp_path / "state.db"
    store.init_db(str(path))
    return str(path)
