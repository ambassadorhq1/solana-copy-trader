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
    from .clusters import ensure_schema
    from .settings import apply
    from .winners import ensure_schema as winners_schema
    from .deep import ensure_schema as deep_schema
    ensure_schema(ctx.db); winners_schema(ctx.db); deep_schema(ctx.db)
    apply(ctx)  # Telegram /set overrides (DB) on top of config.yaml; paper stays forced
    return ctx


async def daily_summary(ctx):
    from .tgbot import PAPER, esc, mark, mult, pct, sol, table
    from .util import day_start_ts
    ex, t0 = ctx.executor, day_start_ts(ctx.cfg)
    d, a = ex.pnl(t0), ex.pnl(0)
    opened = ctx.db.val("SELECT COUNT(*) AS n FROM positions WHERE opened_ts>=?", (t0,), 0)
    wr = lambda x: pct(x["wins"] / x["closed"]) if x["closed"] else "-"
    rows = [["", "SOL", "CLOSED", "WIN%"],
            ["Today", sol(d["realized"]), str(d["closed"]), wr(d)],
            ["All", sol(a["realized"]), str(a["closed"]), wr(a)],
            ["Open", sol(a["unrealized"]), str(len(ex.open_positions())), ""]]
    text = (f"📊 <b>Daily summary</b> · {local_day(ctx.cfg)} · {PAPER}\n" + table(rows)
            + f"{mark(d['realized'])} Entries {opened} · spent {sol(ex.spent_today(), False)} SOL · Helius {ctx.budget.used():,}/mo")
    try:
        from .exits import refresh_reco
        from .tgbot import ladder
        dist, reco = await asyncio.to_thread(refresh_reco, ctx)
        if dist["n"]:
            text += (f"\n📐 Exits: wallet median {mult(dist['median'])} (p25 {mult(dist['p25'])} · p75 {mult(dist['p75'])})"
                     + (f" · TP {esc(ladder(reco['ladder']))} → {mult(reco['expected'])}/trade" if reco.get("ok")
                        else f" · no ladder yet"))
        else:
            text += "\n📐 Exits: no closed trades yet"
    except Exception as e:
        log.exception("exit summary failed: %s", e)
    await ctx.tg.send(text)


STARTUP_SCOUT_STALE_S = 6 * 3600


async def scout_job(ctx):
    """run_scout under ctx.scout_lock; on failure stamp last_scout so we don't re-scout every minute."""
    db = ctx.db
    async with ctx.scout_lock:
        try:
            return await run_scout(ctx)
        finally:
            if (db.kv_get("last_scout") or {}).get("ts", 0) < now() - 60:
                db.kv_set("last_scout", {"ts": now(), "tokens": 0, "new_candidates": 0, "error": True})


def startup_scout(ctx):
    """On boot: start a background scout if last_scout (DB) is older than 6 h. Respects the budget hard stop.
    -> the task, or None when skipped."""
    ls = ctx.db.kv_get("last_scout") or {}
    if (now() - (ls.get("ts") or 0) < STARTUP_SCOUT_STALE_S or ctx.scout_lock.locked() or not ctx.helius.key
            or ctx.budget.over_hard_stop()):
        return None
    log.info("startup scout: last_scout is stale, starting one in the background")

    async def job():
        try:
            await scout_job(ctx)
        except Exception as e:
            log.exception("startup scout failed: %s", e)
    return asyncio.get_running_loop().create_task(job())


async def scheduler_tick(ctx):
    cfg, db, today, hour = ctx.cfg, ctx.db, local_day(ctx.cfg), local_hour(ctx.cfg)
    if ctx.budget.over_hard_stop() and db.kv_get("budget_stop") != today[:7]:
        db.kv_set("budget_stop", today[:7])
        await ctx.watcher.disable_webhook()
        await ctx.tg.send("⚠️ <b>Helius hard stop</b>\nMonthly credit limit reached: scouting and watching paused. Exits still run.")
    ls = db.kv_get("last_scout") or {}
    if now() - (ls.get("ts") or 0) >= cfg["scout"]["interval_min"] * 60 and not ctx.scout_lock.locked() and ctx.helius.key:
        await scout_job(ctx)
    from .winners import format_result, run_winners, wcfg
    wc = wcfg(cfg)
    if (wc["enabled"] and ctx.helius.key and hour >= wc["run_hour"] and db.kv_get("last_winners_day") != today
            and not ctx.scout_lock.locked()):
        db.kv_set("last_winners_day", today)
        async with ctx.scout_lock:
            res = await run_winners(ctx)
        if res["tokens"] or res["scored"]:
            await ctx.tg.send(format_result(res))
    from . import deep
    dc = deep.dcfg(cfg)
    from datetime import datetime
    from .util import tz
    d_now = datetime.now(tz(cfg))
    week = "%d-W%02d" % d_now.isocalendar()[:2]
    if (dc["enabled"] and ctx.helius.key and d_now.weekday() == dc["run_weekday"] and hour >= dc["run_hour"]
            and db.kv_get("last_deep_week") != week and not ctx.scout_lock.locked()):
        db.kv_set("last_deep_week", week)
        async with ctx.scout_lock:
            res = await deep.run_deep(ctx)
        await ctx.tg.send(deep.format_result(res))
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
    from .dashboard import add_routes
    add_routes(app, ctx)  # GET /dashboard + GET /api/summary (read-only, key-protected)

    async def on_start(_):
        if not start_background:
            return
        if ctx.public_url and ctx.tg.token:
            await ctx.tg.set_webhook(ctx.public_url + "/tg", ctx.tg_secret)
        elif ctx.tg.token:
            app["tg_poll"] = asyncio.create_task(ctx.tg.poll_loop())
        await ctx.watcher.sync()
        st = startup_scout(ctx)  # no scout since boot otherwise: last_scout lives in the DB and survives redeploys
        app["tasks"] = ([st] if st else []) + [asyncio.create_task(_loop("scheduler", lambda: scheduler_tick(ctx), 60)),
                        asyncio.create_task(_loop("exits", ctx.executor.check_exits, ctx.cfg["execution"]["exit_check_s"])),
                        asyncio.create_task(ctx.watcher.poll_loop())]
        await ctx.tg.send(f"🚀 <b>Copy-trader started</b> · 📄 PAPER · {ctx.watcher.mode}\n/help")

    async def on_stop(_):
        for t in app.get("tasks", []) + ([app["tg_poll"]] if "tg_poll" in app else []):
            t.cancel()
        await ctx.http.close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app
