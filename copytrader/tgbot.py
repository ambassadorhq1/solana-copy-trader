"""Telegram: alerts + commands. Webhook on /tg when PUBLIC_URL is set, else long polling. Only TELEGRAM_CHAT_ID is obeyed.
Messages use parse_mode=HTML: every dynamic value goes through esc(); tables are <pre> blocks kept ~34 chars wide."""
import asyncio, html, json, re

from .util import log, now, short, day_start_ts

MAX_LEN = 4096
PAPER = "📄 PAPER"

HELP = ("🤖 <b>Copy-trader</b> · " + PAPER + " only\n\n"
        "<b>Overview</b>\n/status · /pnl\n/positions · /wallets\n/clusters — recent smart-money clusters\n\n"
        "<b>Exits</b>\n/exits — stats + suggested TP ladder\n/exits &lt;wallet&gt; — one wallet (prefix ok)\n"
        "/exit &lt;id&gt; follow|2x|3x|now\n\n"
        "<b>Settings</b>\n/settings — current knobs\n/set &lt;key&gt; &lt;value&gt; · /set &lt;key&gt; default\n\n"
        "<b>Control</b>\n/pause · /resume — new entries only\n/scout_winners — seed wallets from recent big pumps\n"
        "/scout_winners deep — 1-year lookback (slow, weekly)\n\n"
        "<i>follow = sell when the wallet sells · 2x/3x = sell all there · now = sell now. "
        "Stop-loss −50% and the 24h time stop always apply.</i>")


# ---------------------------------------------------------------- formatting helpers (also used by executor/app)
def esc(s):
    return html.escape(str(s), quote=False)


def sol(x, sign=True):
    x = float(x or 0)
    if abs(x) < 5e-4:
        return "0.000"
    return f"{x:+.3f}" if sign else f"{x:.3f}"


def pct(x, sign=False):
    """x is a fraction (0.5 -> 50.0%)."""
    return "-" if x is None else (f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%")


def mult(x):
    return "-" if x is None else f"{x:.2f}x"


def mark(x):
    """🟢 profit / 🔴 loss / ⚪ flat."""
    return "🟢" if (x or 0) > 1e-9 else "🔴" if (x or 0) < -1e-9 else "⚪"


def tok(sym, mint=None, n=8):
    s = (sym or "").strip() or short(mint or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def px(x):
    return "-" if not x else f"{x:.3e}"


def ladder(lad):
    """Tranche sizes are allocations (config values), so they stay whole percents: 50%@2x · 50%@3x."""
    if not lad:
        return "no TP (follow/SL/time)"
    parts = [f"{t['frac'] * 100:g}%@{t['mult']:g}x" for t in lad]
    rest = 1 - sum(t["frac"] for t in lad)
    if rest > 1e-3:
        parts.append(f"{rest * 100:g}% runner")
    return " · ".join(parts)


def ago(ts):
    if not ts:
        return "never"
    m = max(now() - ts, 0) // 60
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


def age(ts):
    s = max(now() - (ts or now()), 0)
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 172800 else f"{s // 86400}d"


def table(rows, align=None):
    """rows: list of lists of str (first row = header). Right-aligns all but column 0 unless align says 'l'."""
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    rows = [list(r) + [""] * (n - len(r)) for r in rows]
    w = [max(len(str(r[i])) for r in rows) for i in range(n)]
    align = align or ("l" + "r" * (n - 1))
    out = [" ".join(str(c).ljust(w[i]) if align[i] == "l" else str(c).rjust(w[i]) for i, c in enumerate(r)).rstrip()
           for r in rows]
    return "<pre>" + esc("\n".join(out)) + "</pre>"


def clip(text, limit=MAX_LEN):
    """Trim to Telegram's limit at a line boundary without leaving a <pre>/<b>/<i>/<code> tag open."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 40]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    cut = re.sub(r"<[^>]*$", "", cut)  # half-written tag
    cut = re.sub(r"&[a-z#0-9]*$", "", cut)  # half-written entity
    for t in ("pre", "code", "b", "i"):
        if cut.count(f"<{t}>") > cut.count(f"</{t}>"):
            cut += f"</{t}>"
    return cut + "\n… (trimmed)"


def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


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
        text = clip(text)
        self.sent.append(text)
        log.info("TG> %s", plain(text).replace("\n", " | "))
        if self.token and self.chat_id:
            try:
                r = await self.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                                    disable_web_page_preview=True) or {}
                if r.get("ok") is False:  # bad markup somewhere: never lose the alert, resend as plain text
                    log.warning("telegram HTML rejected (%s), resending plain", r.get("description"))
                    await self.call("sendMessage", chat_id=self.chat_id, text=plain(text)[:MAX_LEN], disable_web_page_preview=True)
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
            db.kv_set("paused", True); return "⏸ <b>Paused</b>\nNo new paper entries. Exits keep running."
        if cmd == "/resume":
            db.kv_set("paused", False); return "▶️ <b>Resumed</b>\nNew paper entries are on."
        if cmd == "/status":
            return self.status()
        if cmd == "/wallets":
            return self.wallets()
        if cmd == "/positions":
            return self.positions()
        if cmd == "/pnl":
            return self.pnl()
        if cmd == "/clusters":
            return self.clusters()
        if cmd == "/settings":
            return self.settings()
        if cmd == "/set":
            from .settings import set_value
            if len(args) < 2:
                return "Usage: <code>/set &lt;key&gt; &lt;value&gt;</code> · keys in /settings"
            ok, msg = set_value(ctx, args[0], " ".join(args[1:]))
            return ("✅ " if ok else "⚠️ ") + esc(msg)
        if cmd in ("/scout_winners", "/scoutwinners"):
            return self.scout_winners(deep=bool(args) and args[0].lower() == "deep")
        if cmd == "/exits":
            from . import exits
            wallet = None
            if args:
                found = exits.resolve_wallet(db, args[0])
                if len(found) != 1:
                    return (f"{'No' if not found else 'Several'} wallets match <code>{esc(args[0])}</code>. "
                            "Use more characters of the address.")
                wallet = found[0]
            dist, reco = await asyncio.to_thread(exits.refresh_reco if not wallet else exits.analyze, ctx, *([wallet] if wallet else []))
            return format_exits(ctx, dist, reco, wallet)
        if cmd == "/exit":
            if len(args) != 2 or not args[0].lstrip("#").isdigit():
                return "Usage: <code>/exit &lt;id&gt; follow|2x|3x|now</code>"
            return esc(await ex.set_exit(int(args[0].lstrip("#")), args[1].lower()))
        return "Unknown command.\n\n" + HELP

    # ---------------------------------------------------------------- command views
    def status(self):
        ctx, ex, db = self.ctx, self.ctx.executor, self.ctx.db
        b, ls = ctx.budget, db.kv_get("last_scout") or {}
        n = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM wallets GROUP BY status")}
        state = "⏸ paused" if ex.paused() else "▶️ running"
        rows = [["Open", f"{len(ex.open_positions())}/{ex.c['max_open']}"],
                ["Spent", f"{sol(ex.spent_today(), False)}/{sol(ex.c['daily_cap_sol'], False)} SOL"],
                ["Watch", f"{n.get('watch', 0)} · bench {n.get('qualified', 0)}"],
                ["Cands", f"{n.get('candidate', 0)} · rej {n.get('rejected', 0)}"],
                ["Helius", f"{b.used(day=_day(ctx)):,} today"],
                ["", f"{b.used():,}/{ctx.cfg['budget']['helius_monthly_credits']:,} mo"],
                ["Scout", f"{ago(ls.get('ts'))} · {ls.get('tokens', 0)} tok · {ls.get('new_candidates', 0)} new"]]
        return f"{PAPER} · {state} · {esc(ctx.watcher.mode)}\n" + table(rows, "ll")

    def wallets(self):
        db = self.ctx.db
        rows = db.all("SELECT address, score, stats FROM wallets WHERE status='watch' ORDER BY score DESC LIMIT 15")
        if not rows:
            return "👀 <b>Watchlist</b>\nNo wallets yet."
        from .deep import insider_set
        ins = insider_set(db)
        t = [["WALLET", "SC", "WR%", "PNL", "7D", ""]]
        for r in rows:
            s = json.loads(r["stats"] or "{}")
            t.append([short(r["address"], 3), f"{r['score'] or 0:.0f}", f"{(s.get('win_rate') or 0) * 100:.1f}",
                      f"{s.get('pnl_sol') or 0:.3f}", f"{s.get('pnl_7d') or 0:+.3f}", "🎯" if r["address"] in ins else ""])
        total = db.val("SELECT COUNT(*) AS n FROM wallets WHERE status='watch'", (), 0)
        dormant = db.val("SELECT COUNT(*) AS n FROM wallets WHERE status='dormant'", (), 0)
        n_ins = sum(r["address"] in ins for r in rows)
        return (f"👀 <b>Watchlist</b> · {total}" + (f" · 🎯 {n_ins}" if n_ins else "") + "\n" + table(t)
                + "<i>PnL in SOL, 30 days" + (" · 🎯 = insider-likely (early on 2+ winners, ranked first)" if n_ins else "") + "</i>"
                + (f"\n💤 Dormant: {dormant} (no swap in 7d, re-checked weekly)" if dormant else ""))

    def positions(self):
        ex = self.ctx.executor
        ps = ex.open_positions()
        if not ps:
            return f"📂 <b>Open positions</b> · {PAPER}\nNone."
        t, notes = [["#", "TOKEN", "MULT", "PNL%", "AGE"]], []
        for p in ps:
            v = position_value(p)
            t.append([str(p["id"]), tok(p["symbol"], p["mint"], 7), mult(v["mult"]), pct(v["pnl_pct"], True), age(p["opened_ts"])])
            lad = ex.ladder_of(p)
            bits = [f"{p['tokens_left'] / p['tokens_init'] * 100:.1f}% left" if p["tokens_init"] else ""]
            bits.append(ladder(lad) if p["exit_mode"] == "default" else f"mode {p['exit_mode']}")
            notes.append(f"#{p['id']} {short(p['wallet'])} · " + " · ".join(b for b in bits if b))
        total = sum(position_value(p)["pnl_sol"] for p in ps)
        return (f"📂 <b>Open positions</b> · {len(ps)}/{ex.c['max_open']} · {PAPER}\n" + table(t, "rlrrr")
                + esc("\n".join(notes)) + f"\n{mark(total)} Open PnL {sol(total)} SOL")

    def pnl(self):
        ex, ctx = self.ctx.executor, self.ctx
        a, d = ex.pnl(0), ex.pnl(day_start_ts(ctx.cfg))
        wr = lambda x: pct(x["wins"] / x["closed"]) if x["closed"] else "-"
        t = [["", "SOL", "CLOSED", "WIN%"],
             ["Today", sol(d["realized"]), str(d["closed"]), wr(d)],
             ["All", sol(a["realized"]), str(a["closed"]), wr(a)],
             ["Open", sol(a["unrealized"]), str(len(ex.open_positions())), ""]]
        return f"{mark(a['realized'])} <b>Paper PnL</b> · {PAPER}\n" + table(t) + "<i>Open = unrealized</i>"


    def settings(self):
        from .settings import KNOBS, overrides, show
        ov = overrides(self.ctx)
        t = [["KEY", "VALUE"]] + [[k + (" *" if k in ov else ""), show(self.ctx, k)] for k in KNOBS]
        return (f"⚙️ <b>Settings</b> · {PAPER} (forced)\n" + table(t, "ll")
                + "<i>* = override saved in the DB (beats config.yaml)</i>\n"
                  "<code>/set trade_sol 0.05</code> · <code>/set stop_loss 40</code>\n"
                  "<code>/set tp_ladder 50@2,50@4</code> · <code>/set &lt;key&gt; default</code>")

    def scout_winners(self, deep=False):
        ctx = self.ctx
        if ctx.scout_lock.locked():
            return "⏳ A scout is already running. Try again in a few minutes."
        if not ctx.helius.key:
            return "⚠️ HELIUS_API_KEY is not set."

        async def job():
            if deep:
                from .deep import format_result, run_deep as run
            else:
                from .winners import format_result, run_winners as run
            try:
                async with ctx.scout_lock:
                    res = await run(ctx, manual=True)
                await self.send(format_result(res))
            except Exception as e:
                log.exception("winners scout failed: %s", e)
                await self.send(f"⚠️ Winners scout failed: {esc(e)}")
        self.last_task = asyncio.get_running_loop().create_task(job())
        if deep:
            return "🔭 <b>Deep winners scout started</b> (1-year lookback)\nSlow and credit-capped; the result lands here."
        return "🏆 <b>Winners scout started</b>\nI'll post the result here (a few minutes)."

    def clusters(self):
        from .clusters import ccfg, recent
        c, rows = ccfg(self.ctx.cfg), recent(self.ctx.db, 10)
        rule = (f"≥{c['min_wallets']} wallets in {c['window_min']}m, or ≥{c['min_wallets_for_amount']} with "
                f"≥{c['min_total_sol']:.3f} SOL · token &lt;{c['max_token_age_h']}h")
        if not rows:
            return f"🧠 <b>Smart-money clusters</b>\nNone yet.\n<i>{rule}{'' if c['enabled'] else ' · DISABLED'}</i>"
        t = [["TOKEN", "W", "SOL", "SEEN"]] + [[tok(r["symbol"], r["mint"], 8), str(r["wallets"]), f"{r['total_sol']:.3f}",
                                                ago(r["last_ts"])] for r in rows]
        links = " · ".join(f'<a href="{esc(r["dex_url"])}">{esc(tok(r["symbol"], r["mint"], 8))}</a>' for r in rows)
        return f"🧠 <b>Smart-money clusters</b> · last {len(rows)}\n" + table(t) + links + f"\n<i>{rule} · alert only</i>"


def position_value(p):
    """Current multiple of the price and trade PnL (realized sells + what's left at the last price)."""
    left_val = (p["tokens_left"] / 10 ** (p["decimals"] or 0)) * (p["last_price"] or 0)
    m = (p["last_price"] or 0) / p["entry_price"] if p["entry_price"] else None
    cost_left = p["sol_in"] * p["tokens_left"] / p["tokens_init"] if p["tokens_init"] else 0
    pnl_sol = (p["sol_out"] or 0) + left_val - (p["sol_in"] or 0)
    return {"mult": m, "pnl_sol": pnl_sol, "pnl_pct": pnl_sol / p["sol_in"] if p["sol_in"] else None,
            "unrealized": left_val - cost_left, "value_left": left_val}


def format_exits(ctx, dist, reco, wallet=None):
    from .exits import config_ladder, ecfg
    e = ecfg(ctx.cfg)
    who = f"<code>{esc(short(wallet))}</code>" if wallet else f"watchlist ({dist['wallets']} wallet{'s' if dist['wallets'] != 1 else ''})"
    if not dist["n"]:
        return f"📐 <b>Exits</b> · {who}\nNo closed trades yet."
    lines = [f"📐 <b>Exits</b> · {who}",
             f"{dist['n_wallet_trades']} wallet + {dist['n_paper']} paper trades closed",
             f"Wallet exits: p25 {mult(dist['p25'])} · median {mult(dist['median'])} · p75 {mult(dist['p75'])}"]
    t = [["LEVEL", "WALLET", "US"]]
    for L in e["hit_levels"]:
        wh, oh = dist["wallet_hit"].get(L), dist["our_hit"].get(L)
        t.append([f"≥{L:g}x", pct(wh), pct(oh)])
    lines.append(table(t) + f"<i>Hit rate. US = 24h peak after our entry (wallet +{dist['delay'] * 100:.1f}%), "
                            f"{dist['n_peak']} trades, {dist['peak_from_samples']} from our samples; swap prices are a lower bound.</i>")
    if reco.get("ok"):
        cur, fo = reco.get("current") or {}, reco.get("follow_only") or {}
        lines += ["", f"✅ <b>Suggested ladder</b>: {esc(ladder(reco['ladder']))}",
                  f"Sim {mult(reco['expected'])}/trade · median {mult(reco['median'])} · losers {pct(reco['p_loss'])}",
                  f"1st tranche hit {pct(reco['first_hit'])} · now {esc(ladder(config_ladder(ctx.cfg['execution'])))}: "
                  f"{mult(cur.get('mean'))} · follow-only {mult(fo.get('mean'))}"]
        if fo.get("mean") and fo["mean"] > reco["expected"]:
            lines.append("<i>Follow-only simulated higher; the ladder banks profit on most trades and our exits lag the wallet's.</i>")
        lines.append("<pre>" + esc(reco["config"]) + "</pre>")
    else:
        lines += ["", f"No ladder suggested: {esc(reco.get('reason'))}"]
    lines.append(f"auto_tune_exits: {'on' if e['auto_tune_exits'] else 'off'}")
    return "\n".join(lines)


def _day(ctx):
    from .util import local_day
    return local_day(ctx.cfg)


_ago = ago  # backwards-compatible name
