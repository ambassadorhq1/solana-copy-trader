"""One process: aiohttp server (/health, /tg, /helius) + background loops (scout, watcher, exits, daily jobs)."""
import asyncio, hashlib, hmac, os, time

from aiohttp import web

from .clients import Budget, Dexscreener, Helius, Http, Jupiter
from .db import DB
from .executor import PaperExecutor
from .scorer import daily_rescore
from .scout import run_scout
from .tgbot import Telegram
from .util import local_day, local_hour, log, now
from .watcher import Watcher


class Context:
    pass


def build_context(cfg, db=None, helius=None, jup=None, dex=None, env=None):
    env = env if env is not None else os.environ
    if cfg.get("mode", "paper") != "paper":
        from .live import LiveExecutor
        LiveExecutor()  # raises: live is disabled in v1
    ctx = Context()
    ctx.cfg, ctx.started = cfg, time.time()
    ctx.db = db or DB(env.get("DATABASE_URL") or "sqlite:///copytrader.db")
    ctx.http = Http()
    ctx.budget = Budget(ctx.db, cfg)
    key = env.get("HELIUS_API_KEY", "")
    ctx.public_url = (env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    ctx.helius_secret = env.get("HELIUS_WEBHOOK_SECRET") or hashlib.sha256(f"helius:{key}".encode()).hexdigest()[:32]
    tok = env.get("TELEGRAM_BOT_TOKEN", "")
    ctx.tg_secret = env.get("TG_WEBHOOK_SECRET") or hashlib.sha256(f"tg:{tok}".encode()).hexdigest()[:32]
    ctx.helius = helius or Helius(key, cfg, ctx.budget, ctx.http)
    ctx.jup = jup or Jupiter(cfg, ctx.http, env.get("JUP_API_KEY") or None)
    ctx.dex = dex or Dexscreener(cfg, ctx.http)
    ctx.tg = Telegram(ctx, tok, env.get("TELEGRAM_CHAT_ID", ""), ctx.http)
    ctx.executor = PaperExecutor(ctx)
    ctx.watcher = Watcher(ctx)
    ctx.scout_lock = asyncio.Lock()
    return ctx


async def daily_summary(ctx):
    ex = ctx.executor
    from .util import day_start_ts
    d, a = ex.pnl(day_start_ts(ctx.cfg)), ex.pnl(0)
    opened = ctx.db.val("SELECT COUNT(*) AS n FROM positions WHERE opened_ts>=?", (day_start_ts(ctx.cfg),), 0)
    try:
        from .exits import summary_line
        exit_line = "\n" + await asyncio.to_thread(summary_line, ctx)
    except Exception as e:
        log.exception("exit summary failed: %s", e); exit_line = ""
    await ctx.tg.send(f"📊 Daily paper summary {local_day(ctx.cfg)}\nEntries {opened} · spent {ex.spent_today():.2f} SOL\n"
                      f"Realized today {d['realized']:+.4f} SOL ({d['closed']} closed, {d['wins']} wins)\n"
                      f"All-time realized {a['realized']:+.4f} SOL · unrealized {a['unrealized']:+.4f} SOL\n"
                      f"Open {len(ex.open_positions())} · Helius credits this month {ctx.budget.used()}" + exit_line)


async def scheduler_tick(ctx):
    cfg, db, today, hour = ctx.cfg, ctx.db, local_day(ctx.cfg), local_hour(ctx.cfg)
    if ctx.budget.over_hard_stop() and db.kv_get("budget_stop") != today[:7]:
        db.kv_set("budget_stop", today[:7])
        await ctx.watcher.disable_webhook()
        await ctx.tg.send("⚠️ Helius credit hard stop reached for this month: scouting/watching paused, exits still run.")
    ls = db.kv_get("last_scout") or {}
    if now() - (ls.get("ts") or 0) >= cfg["scout"]["interval_min"] * 60 and not ctx.scout_lock.locked() and ctx.helius.key:
        async with ctx.scout_lock:
            try:
                await run_scout(ctx)
            finally:  # never re-scout every minute after a failure
                if (db.kv_get("last_scout") or {}).get("ts", 0) < now() - 60:
                    db.kv_set("last_scout", {"ts": now(), "tokens": 0, "new_candidates": 0, "error": True})
    if hour >= cfg["scoring"]["rescore_hour"] and db.kv_get("last_rescore") != today:
        db.kv_set("last_rescore", today); await daily_rescore(ctx)
        from .exits import prune_samples
        prune_samples(ctx)
    if hour >= cfg["daily_summary_hour"] and db.kv_get("last_summary") != today:
        db.kv_set("last_summary", today); await daily_summary(ctx)


async def _loop(name, fn, every):
    while True:
        try:
            await fn()
        except Exception as e:
            log.exception("%s loop error: %s", name, e)
        await asyncio.sleep(every)


def make_app(ctx, start_background=True):
    app = web.Application()

    async def health(_):
        ex = ctx.executor
        return web.json_response({"ok": True, "mode": "paper", "paused": ex.paused(), "watcher": ctx.watcher.mode,
                                  "open_positions": len(ex.open_positions()),
                                  "watchlist": ctx.db.val("SELECT COUNT(*) AS n FROM wallets WHERE status='watch'", (), 0),
                                  "credits_month": ctx.budget.used(), "uptime_s": int(time.time() - ctx.started)})

    async def tg(req):
        if not hmac.compare_digest(req.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), ctx.tg_secret):
            return web.Response(status=401)
        asyncio.create_task(ctx.tg.handle_update(await req.json()))
        return web.Response(text="ok")

    async def helius(req):
        if not hmac.compare_digest(req.headers.get("Authorization", ""), ctx.helius_secret):
            return web.Response(status=401)
        asyncio.create_task(ctx.watcher.handle_webhook(await req.json()))
        return web.Response(text="ok")  # ack fast: Helius retries / disables slow endpoints

    async def root(_):
        return web.Response(text="copytrader paper v1")

    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_post("/tg", tg)
    app.router.add_post("/helius", helius)

    async def on_start(_):
        if not start_background:
            return
        if ctx.public_url and ctx.tg.token:
            await ctx.tg.set_webhook(ctx.public_url + "/tg", ctx.tg_secret)
        elif ctx.tg.token:
            app["tg_poll"] = asyncio.create_task(ctx.tg.poll_loop())
        await ctx.watcher.sync()
        app["tasks"] = [asyncio.create_task(_loop("scheduler", lambda: scheduler_tick(ctx), 60)),
                        asyncio.create_task(_loop("exits", ctx.executor.check_exits, ctx.cfg["execution"]["exit_check_s"])),
                        asyncio.create_task(ctx.watcher.poll_loop())]
        await ctx.tg.send(f"🚀 Copy-trader started (PAPER, watcher={ctx.watcher.mode}). /help")

    async def on_stop(_):
        for t in app.get("tasks", []) + ([app["tg_poll"]] if "tg_poll" in app else []):
            t.cancel()
        await ctx.http.close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app
