"""NONCE Genesis mint watcher: GET /nonce-watch (hit by cron-job.org every 2 min). Alert-only, no trading."""
import asyncio, json, time

import aiohttp
from aiohttp import web

from .util import log

MCP = "https://mcp.nonceprotocol.xyz/mcp"
REALERT_S = 600
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _parse(text):
    """Body may be plain JSON or SSE ('data: {...}')."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                pass
    return None


async def fetch_info():
    """Return protocol_info dict, or None when unknown (busy/error)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "sol-copytrader", "version": "1"}}}
            async with s.post(MCP, json=init, headers=HDR) as r:
                sid = r.headers.get("Mcp-Session-Id")
                await r.text()
            h = dict(HDR, **({"Mcp-Session-Id": sid} if sid else {}))
            async with s.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h) as r:
                await r.text()
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "protocol_info", "arguments": {}}}
            async with s.post(MCP, json=call, headers=h) as r:
                body = _parse(await r.text())
        if not body or "result" not in body:
            return None  # "Service busy" etc. -> unknown
        info = json.loads(body["result"]["content"][0]["text"])
        if not isinstance(info, dict) or "error" in info:
            return None
        return info
    except Exception as e:
        log.warning("nonce-watch fetch failed: %s", e)
        return None


async def check(ctx, info=None, now=None):
    info = info if info is not None else await fetch_info()
    now = now or time.time()
    if info is None:
        return "unknown"
    adm = str(info.get("admission", "")).lower()
    st = ctx.db.kv_get("nonce_state") or {}
    if adm == "open":
        last = st.get("last_alert", 0) if st.get("admission") == "open" else 0
        if now - last >= REALERT_S:
            rem = info.get("remainingSupply", "?")
            await ctx.tg.send(f"NONCE mint is OPEN now. Open Hark and say 'mint NONCE' right away. (remainingSupply: {rem})")
            last = now
        ctx.db.kv_set("nonce_state", {"admission": "open", "last_alert": last})
    else:
        ctx.db.kv_set("nonce_state", {"admission": adm or "closed", "last_alert": 0})
    return adm or "closed"


def add_routes(app, ctx):
    app["nonce_lock"] = asyncio.Lock()

    async def run():
        if app["nonce_lock"].locked():
            return
        async with app["nonce_lock"]:
            try:
                await check(ctx)
            except Exception as e:
                log.warning("nonce-watch error: %s", e)

    async def watch(req):
        if req.query.get("test") == "1" and req.query.get("key") == ctx.tg_secret:
            asyncio.create_task(ctx.tg.send("NONCE watcher is live ✅"))
        asyncio.create_task(run())
        st = ctx.db.kv_get("nonce_state") or {}
        return web.json_response({"ok": True, "last": st.get("admission")})

    app.router.add_get("/nonce-watch", watch)
