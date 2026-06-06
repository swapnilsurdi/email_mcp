import asyncio
import json

import httpx

from email_mcp.http import db, status


def test_report_loop_posts_green_beats(tmp_path):
    p = str(tmp_path / "state.db")
    db.init_http_tables(p)
    db.set_service_identity(p, "matrix_user", "@emailer:chat.test")
    captured = []

    def handler(request):
        captured.append((request.url.path, request.headers.get("authorization"),
                         json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await status.report_loop(lambda: p, "https://email-mcp.test",
                                 "http://hub.test", "tok-cherry",
                                 status_id="cherry-email-mcp-http",
                                 interval=0, client=client, iterations=2)
        await client.aclose()
    asyncio.run(run())

    assert len(captured) == 2
    path, auth, body = captured[0]
    assert path == "/service/report" and auth == "Bearer tok-cherry"
    assert body["id"] == "cherry-email-mcp-http" and body["status"] == "green"
    assert body["ttl_s"] == 300
    assert body["data"]["matrix"] == "@emailer:chat.test"
    assert body["data"]["mcp"] == "https://email-mcp.test/mcp"


def test_report_loop_red_on_db_failure_and_survives_hub_errors(tmp_path):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(503, json={"error": "down"})

    def broken_db():
        raise RuntimeError("disk gone")

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await status.report_loop(broken_db, "https://email-mcp.test",
                                 "http://hub.test", None, status_id="cherry-x",
                                 interval=0, client=client, iterations=1)
        await client.aclose()
    asyncio.run(run())

    [body] = captured
    assert body["status"] == "red"
    assert body["data"]["matrix"] == "(registering)"
