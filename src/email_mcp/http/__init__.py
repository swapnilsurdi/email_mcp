"""The multi-tenant HTTP service for email-mcp.

A long-lived FastAPI app that exposes the same email tools over Streamable-HTTP MCP
(gated by scoped agent keys), plus a web dashboard and a Matrix onboarding bot. It is
entirely separate from the stdio server (`email_mcp.server`); importing this package is
optional and only needed when the `[http]` extra is installed. The core email logic
(`email_ops`, `security`, providers, ledger, caches) is reused unchanged.
"""
