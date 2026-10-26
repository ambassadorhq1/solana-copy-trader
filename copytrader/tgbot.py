"""Telegram: alerts + commands. Webhook on /tg when PUBLIC_URL is set, else long polling. Only TELEGRAM_CHAT_ID is obeyed."""
import asyncio, json

from .util import log, now, short, day_start_ts

HELP = ("/status /wallets /positions /pnl\n/exit <id> follow|2x|3x|now\n/exits [wallet] — exit stats + suggested TP ladder\n/pause /resume\n"
        "Paper mode only. Exit modes: follow = sell when the wallet sells; 2x/3x = sell all there; now = sell now. "
        "Stop-loss -50% and 24h time stop always apply.")


class Telegram:
    def __init__(self, ctx, token, chat_id, http):
        self.ctx, self.token, self.chat_id, self.http = ctx, token, str(chat_id or ""), http
        self.api = f"https://api.telegram.org/bot{token}/"
        self.sent = []  # kept for tests / dry-run

    async def call(self, method, **params):
        if not self.token:
            return None
        s = await self.http.s()
        async with s.post(self.api + method, json=params) as r:
            return await r.json(content_type=None)

    async def send(self, text):
        self.sent.append(text)
        log.info("TG> %s", text.replace("\n", " | "))
        if self.token and self.chat_id:
            try:
                await self.call("sendMessage", chat_id=self.chat_id, text=text[:4000], disable_web_page_preview=True)
            except Exception as e:
                log.warning("telegram send failed: %s", e)

    async def set_webhook(self, url, secret):
        r = await self.call("setWebhook", url=url, secret_token=secret, allowed_updates=["message"])
        log.info("telegram setWebhook: %s", r)

    async def poll_loop(self):
        await self.call("deleteWebhook")
        offset = None
        while True:
            try:
                r = await self.call("getUpdates", timeout=25, offset=offset) or {}
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    await self.handle_update(u)
            except Exception as e:
                log.warning("telegram poll: %s", e); await asyncio.sleep(5)

    async def handle_update(self, u):
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != self.chat_id or not m.get("text", "").startswith("/"):
            return
        reply = await self.command(m["text"])
        if reply:
            await self.send(reply)

    async def command(self, text):
        parts = text.strip().split()
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        ctx, ex, db = self.ctx, self.ctx.executor, self.ctx.db
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/pause":
            db.kv_set("paused", True); return "⏸ Paused: no new paper entries (exits keep running)."
        if cmd == "/resume":
            db.kv_set("paused", False); return "▶️ Resumed."
        if cmd == "/status":
            b, ls = ctx.budget, db.kv_get("last_scout") or {}
            n = lambda st: db.val("SELECT COUNT(*) AS n FROM wallets WHERE status=?", (st,), 0)
            return (f"Mode: PAPER · {'PAUSED' if ex.paused() else 'running'} · watcher: {ctx.watcher.mode}\n"
                    f"Open {len(ex.open_positions())}/{ex.c['max_open']} · spent today {ex.spent_today():.2f}/{ex.c['daily_cap_sol']} SOL\n"
                    f"Wallets: watch {n('watch')} · bench {n('qualified')} · candidates {n('candidate')} · rejected {n('rejected')}\n"
                    f"Helius credits: today {b.used(day=_day(ctx))} · month {b.used()} / {ctx.cfg['budget']['helius_monthly_credits']}\n"
                    f"Last scout: {_ago(ls.get('ts'))} ({ls.get('tokens', 0)} tokens, {ls.get('new_candidates', 0)} new)")
        if cmd == "/wallets":
            rows = db.all("SELECT address, score, stats FROM wallets WHERE status='watch' ORDER BY score DESC LIMIT 15")
            if not rows:
                return "No wallets on the watchlist yet."
            out = []
            for r in rows:
                s = json.loads(r["stats"] or "{}")
                out.append(f"{short(r['address'])} score {r['score']:.0f} · {s.get('closed')}tr WR {s.get('win_rate', 0):.0%} "
                           f"PnL {s.get('pnl_sol', 0):.0f} · 7d {s.get('pnl_7d', 0):+.1f}")
            return "👀 Watchlist\n" + "\n".join(out)
        if cmd == "/positions":
            ps = ex.open_positions()
            if not ps:
                return "No open paper positions."
            return "📂 Open\n" + "\n".join(
                f"#{p['id']} {p['symbol']} {(p['last_price'] or 0) / p['entry_price']:.2f}x · "
                f"{p['tokens_left'] / p['tokens_init']:.0%} left · {p['exit_mode']} · {(now() - p['opened_ts']) // 3600}h" for p in ps)
        if cmd == "/pnl":
            a, d = ex.pnl(0), ex.pnl(day_start_ts(ctx.cfg))
            wr = f"{a['wins'] / a['closed']:.0%}" if a["closed"] else "-"
            return (f"💰 Paper PnL\nToday realized {d['realized']:+.4f} SOL\nAll-time realized {a['realized']:+.4f} SOL · "
                    f"{a['closed']} closed, win rate {wr}\nUnrealized {a['unrealized']:+.4f} SOL")
        if cmd == "/exits":
            from . import exits
            wallet = None
            if args:
                found = exits.resolve_wallet(db, args[0])
                if len(found) != 1:
                    return f"{'No' if not found else 'Several'} wallets match {args[0]}. Use more characters of the address."
                wallet = found[0]
            dist, reco = await asyncio.to_thread(exits.refresh_reco if not wallet else exits.analyze, ctx, *([wallet] if wallet else []))
            return exits.format_exits(ctx, dist, reco, wallet)
        if cmd == "/exit":
            if len(args) != 2 or not args[0].lstrip("#").isdigit():
                return "Usage: /exit <id> follow|2x|3x|now"
            return await ex.set_exit(int(args[0].lstrip("#")), args[1].lower())
        return "Unknown command.\n" + HELP


def _day(ctx):
    from .util import local_day
    return local_day(ctx.cfg)


def _ago(ts):
    if not ts:
        return "never"
    m = (now() - ts) // 60
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"
