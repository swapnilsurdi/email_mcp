"""Heartbeat to the fleet status hub (status.surdi.in): registers the service row
(default id `cherry-email-mcp-http`) and beats every minute, green/red from the same
DB check as /health. The bot's Matrix handle rides in `data` so the dashboard shows
who to DM. Ingest contract: the launchlab repo's status/HELP.md (`POST /service/report`,
box bearer token for the trusted badge)."""
import asyncio
import os

import httpx

from email_mcp.http import db, info

DEFAULT_ID = "cherry-email-mcp-http"


def build_payload(status_id, base_url, matrix_user, ok):
    return {
        "id": status_id,
        "name": "email-mcp",
        "details": ("Multi-tenant email for agents — MCP over streamable-HTTP with "
                    "scoped keys, web dashboard, Matrix onboarding."),
        "status": "green" if ok else "red",
        "ttl_s": 300,
        "data": {"url": base_url, "mcp": f"{base_url}/mcp",
                 "matrix": matrix_user or "(registering)",
                 "version": info.VERSION},
    }


async def report_once(client, hub_url, token, payload):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await client.post(hub_url.rstrip("/") + "/service/report",
                          headers=headers, json=payload)
    return r.status_code


async def report_loop(db_path_fn, base_url, hub_url, token, status_id=None,
                      interval=60, client=None, iterations=None):
    """Beat forever (or `iterations` times, for tests). A hub outage is not our
    outage — failed beats are dropped and the next tick tries again."""
    status_id = status_id or os.environ.get("EMAIL_MCP_STATUS_ID", DEFAULT_ID)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    n = 0
    try:
        while iterations is None or n < iterations:
            n += 1
            try:
                dbp = db_path_fn()
                db.init_http_tables(dbp)
                handle, ok = db.get_service_identity(dbp, "matrix_user"), True
            except Exception:
                handle, ok = None, False
            try:
                await report_once(client, hub_url, token,
                                  build_payload(status_id, base_url, handle, ok))
            except Exception:
                pass
            if iterations is None or n < iterations:
                await asyncio.sleep(interval)
    finally:
        if own_client:
            await client.aclose()
