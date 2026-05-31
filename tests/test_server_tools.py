import os
from email_mcp import runtime


def test_paths_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("EMAIL_MCP_ACCOUNTS", str(tmp_path / "accounts.yml"))
    assert runtime.db_path().endswith("s.db")
    assert runtime.accounts_file().endswith("accounts.yml")


def test_server_module_imports():
    import email_mcp.server as srv
    # FastMCP app object exists and tools are registered
    assert hasattr(srv, "mcp")
