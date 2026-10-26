"""Paper executor: risk caps + pre-trade checks, simulated fills at Jupiter quotes, exit engine."""
import asyncio, json

from .exits import config_ladder, entry_ladder, fmt_ladder
from .tgbot import esc, ladder, mark, mult, position_kb, px, sol, tok
from .util import WSOL, log, now, short, day_start_ts

EXIT_MODES = ("default", "follow", "2x", "3x", "now")


class PaperExecutor:
    def __init__(self, ctx):
        self.ctx, self.db, self.c = ctx, ctx.db, ctx.cfg["execution"]
        self._lock = asyncio.Lock()

    # ---------- helpers ----------
    def paused(self):
        return bool(self.db.kv_get("paused", False))

    def spent_today(self):
        return float(self.db.val("SELECT COALESCE(SUM(sol_in),0) AS s FROM positions WHERE opened_ts>=?",
                                 (day_start_ts(self.ctx.cfg),), 0))

    def open_positions(self):
        return self.db.all("SELECT * FROM positions WHERE status='open' ORDER BY id")

    def skip(self, s, why):
        msg = f"skip {short(s['mint'])} from {short(s['wallet'])}: {why}"
        log.info(msg); self.db.event("skip", msg)

    # ---------- signals ----------
    async def on_signal(self, s):
        async with self._lock:
            if s["side"] == "buy":
                await self._buy(s)
            elif s["side"] == "sell":
                await self._follow_sell(s)
        if s["side"] == "buy":  # smart-money cluster alert (alert only, never buys); outside the lock
            try:
                from .clusters import on_buy
                await on_buy(self.ctx, s)
            except Exception as e:
                log.exception("cluster check failed: %s", e)

    async def _buy(self, s):
        c, ctx = self.c, self.ctx
        w = self.db.one("SELECT status FROM wallets WHERE address=?", (s["wallet"],))
        if not w or w["status"] != "watch":
            return
        if self.paused():
            return self.skip(s, "paused")
        if s["sol"] < ctx.cfg["watch"]["min_wallet_buy_sol"]:
            return self.skip(s, f"dust buy {s['sol']:.3f} SOL")
        if now() - (s["ts"] or now()) > ctx.cfg["watch"]["max_signal_age_s"]:
            return self.skip(s, "stale signal")
        if self.db.one("SELECT 1 AS x FROM positions WHERE mint=?", (s["mint"],)):
            return self.skip(s, "already entered this token")
        if len(self.open_positions()) >= c["max_open"]:
            return self.skip(s, "max open positions")
        if self.spent_today() + c["per_trade_sol"] > c["daily_cap_sol"] + 1e-9:
            return self.skip(s, "daily cap")
        pair = (await ctx.dex.best_pairs([s["mint"]])).get(s["mint"])
        liq = ((pair or {}).get("liquidity") or {}).get("usd") or 0
        if liq < c["min_liquidity_usd"]:
            return self.skip(s, f"liquidity ${liq:,.0f}")
        if c["require_authorities_revoked"]:
            info = await ctx.helius.mint_info(s["mint"], "exec")
            if info is None or info.get("mintAuthority") or info.get("freezeAuthority"):
                return self.skip(s, "mint/freeze authority not revoked")
        lamports = int(c["per_trade_sol"] * 1e9)
        q = await ctx.jup.quote(WSOL, s["mint"], lamports, c["slippage_bps"])
        if not q:
            return self.skip(s, "no Jupiter route")
        if float(q.get("priceImpactPct") or 0) > c["max_price_impact"]:
            return self.skip(s, f"price impact {float(q['priceImpactPct']):.0%}")
        out = int(int(q["outAmount"]) * (1 - c["sim_slippage_bps"] / 1e4))
        dec = s["decimals"]
        cost = c["per_trade_sol"] + c["sim_fee_sol"]
        our_px = cost / (out / 10 ** dec)
        wallet_px = s["sol"] / (s["tokens"] / 10 ** dec)
        if our_px > c["max_entry_vs_wallet"] * wallet_px:
            return self.skip(s, f"chasing: {our_px / wallet_px:.2f}x wallet entry")
        back = await ctx.jup.quote(s["mint"], WSOL, out, c["slippage_bps"])  # honeypot / sellability
        if not back or int(back["outAmount"]) / 1e9 < c["per_trade_sol"] * (1 - c["max_roundtrip_loss"]):
            return self.skip(s, "sell simulation failed (honeypot / no exit liquidity)")
        sym = ((pair or {}).get("baseToken") or {}).get("symbol") or short(s["mint"])
        ladder_, lsrc = await entry_ladder(ctx)  # fixed for this trade's life; auto-tune only affects new trades
        pid = self.db.insert("INSERT INTO positions (mint, symbol, wallet, opened_ts, sol_in, tokens_init, tokens_left, decimals, "
                             "entry_price, wallet_price, last_price, tp_ladder, exit_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (s["mint"], sym, s["wallet"], now(), cost, out, out, dec, our_px, wallet_px, our_px, json.dumps(ladder_),
                              "follow" if c.get("default_exit") == "follow" else "default"))
        self.db.x("INSERT INTO price_samples (mint, ts, price) VALUES (?,?,?)", (s["mint"], now(), our_px))
        self.db.x("INSERT INTO fills (position_id, ts, side, sol, tokens, reason, source) VALUES (?,?,?,?,?,?,?)",
                  (pid, now(), "buy", cost, out, f"copy {short(s['wallet'])}", "jupiter"))
        await ctx.watcher.sync()
        from .deep import is_insider
        tag = " · 🎯 insider-likely" if is_insider(self.db, s["wallet"]) else ""
        await ctx.tg.send(f"📄 <b>PAPER BUY #{pid}</b> · {esc(tok(sym, s['mint']))}{tag}\n"
                          f"{sol(cost, False)} SOL @ <code>{px(our_px)}</code> · wallet {esc(short(s['wallet']))} paid "
                          f"<code>{px(wallet_px)}</code> ({sol(s['sol'], False)} SOL)\n"
                          f"Liq ${liq:,.0f} · TP {esc(ladder(ladder_))} [{esc(lsrc)}]\n"
                          + (f"Exit: follow wallet\n" if c.get("default_exit") == "follow" else "")
                          + f"SL −50% · 24h stop · <code>/exit {pid} follow|2x|3x|now</code>", markup=position_kb(pid))

    async def _follow_sell(self, s):
        for p in self.db.all("SELECT * FROM positions WHERE status='open' AND wallet=? AND mint=? AND exit_mode IN ('default','follow')",
                             (s["wallet"], s["mint"])):
            pct = min(1.0, s["tokens"] / s["pre_tokens"]) if s["pre_tokens"] else 1.0
            await self.sell(p, 1.0 if pct >= 0.98 else pct, f"follow: wallet sold {pct * 100:.1f}%")

    # ---------- exits ----------
    async def sell(self, p, frac, reason):
        c, ctx = self.c, self.ctx
        amount = int(p["tokens_left"] * frac)
        if amount <= 0:
            return
        q = await ctx.jup.quote(p["mint"], WSOL, amount, c["slippage_bps"])
        if q:
            sol_ = int(q["outAmount"]) * (1 - c["sim_slippage_bps"] / 1e4) / 1e9 - c["sim_fee_sol"]
            src = "jupiter"
        else:  # no route: value at Dexscreener with a haircut, or zero if the token is gone
            dpx = await ctx.dex.price_sol((await ctx.dex.best_pairs([p["mint"]])).get(p["mint"]))
            sol_ = (amount / 10 ** p["decimals"]) * dpx * (1 - c["fallback_haircut"]) if dpx else 0.0
            src = "dexscreener-est" if dpx else "no-route"
        sol_ = max(sol_, 0.0)
        cost_basis = p["sol_in"] * amount / p["tokens_init"]
        pnl = sol_ - cost_basis
        left = p["tokens_left"] - amount
        closed = left <= p["tokens_init"] * 0.001
        self.db.x("INSERT INTO fills (position_id, ts, side, sol, tokens, pnl, reason, source) VALUES (?,?,?,?,?,?,?,?)",
                  (p["id"], now(), "sell", sol_, amount, pnl, reason, src))
        self.db.x("UPDATE positions SET tokens_left=?, sol_out=sol_out+?, status=?, closed_ts=? WHERE id=?",
                  (0 if closed else left, sol_, "closed" if closed else "open", now() if closed else None, p["id"]))
        p.update(tokens_left=0 if closed else left, sol_out=p["sol_out"] + sol_, status="closed" if closed else "open")
        tot = p["sol_out"] - p["sol_in"]
        head = f"{mark(pnl)} <b>PAPER SELL #{p['id']}</b> · {esc(tok(p['symbol'], p['mint']))} · {frac * 100:.1f}% of rest"
        body = f"{esc(reason)}\n{sol(sol_, False)} SOL ({esc(src)}) · PnL {sol(pnl)} SOL"
        tail = (f"\n{mark(tot)} Closed · total {sol(tot)} SOL ({mult(p['sol_out'] / p['sol_in'])}) · 📄"
                if closed and p["sol_in"] else "")
        await ctx.tg.send(head + "\n" + body + tail)
        if closed:
            await ctx.watcher.sync()

    async def check_exits(self):
        async with self._lock:
            ps = self.open_positions()
            if not ps:
                return
            pairs = await self.ctx.dex.best_pairs([p["mint"] for p in ps])
            c, t = self.c, now()
            for p in ps:
                px = await self.ctx.dex.price_sol(pairs.get(p["mint"]))
                mult = px / p["entry_price"] if px else None
                if px:
                    self.db.x("UPDATE positions SET last_price=?, max_mult=? WHERE id=?", (px, max(p["max_mult"] or 1, mult), p["id"]))
                    self.db.x("INSERT INTO price_samples (mint, ts, price) VALUES (?,?,?)", (p["mint"], t, px))
                mode = p["exit_mode"]
                if mode == "now":
                    await self.sell(p, 1.0, "manual exit now"); continue
                if t - p["opened_ts"] >= c["time_stop_h"] * 3600:
                    await self.sell(p, 1.0, f"time stop {c['time_stop_h']}h"); continue
                if mult is None:
                    continue
                if mult <= c["stop_loss_mult"]:
                    await self.sell(p, 1.0, f"stop-loss {mult:.2f}x"); continue
                if mode == "2x" and mult >= 2:
                    await self.sell(p, 1.0, f"override 2x hit ({mult:.2f}x)")
                elif mode == "3x" and mult >= 3:
                    await self.sell(p, 1.0, f"override 3x hit ({mult:.2f}x)")
                elif mode == "default":
                    ladder = self.ladder_of(p)
                    done, reached, frac = p["tp1_done"] or 0, p["tp1_done"] or 0, 0.0
                    while reached < len(ladder) and mult >= ladder[reached]["mult"]:
                        frac += ladder[reached]["frac"]; reached += 1
                    if reached > done:
                        cum = sum(x["frac"] for x in ladder[:reached])
                        # tranche sizes are fractions of the INITIAL position; the last one sells whatever is left
                        sell_frac = 1.0 if cum >= 0.999 else min(frac * p["tokens_init"] / p["tokens_left"], 1.0)
                        self.db.x("UPDATE positions SET tp1_done=? WHERE id=?", (reached, p["id"]))
                        await self.sell(p, sell_frac, f"TP{reached} {mult:.2f}x")

    def ladder_of(self, p):
        try:
            return json.loads(p.get("tp_ladder") or "null") or config_ladder(self.c)
        except ValueError:
            return config_ladder(self.c)

    async def set_exit(self, pid, mode):
        if mode not in EXIT_MODES:
            return f"mode must be one of {', '.join(EXIT_MODES[1:])}"
        p = self.db.one("SELECT * FROM positions WHERE id=? AND status='open'", (pid,))
        if not p:
            return f"no open position #{pid}"
        self.db.x("UPDATE positions SET exit_mode=? WHERE id=?", (mode, pid))
        if mode == "now":
            async with self._lock:
                await self.sell(p, 1.0, "manual exit now")
            return f"#{pid} closed"
        return f"#{pid} exit mode -> {mode} (stop-loss and 24h time stop stay on)"

    # ---------- reporting ----------
    def pnl(self, since=0):
        r = self.db.one("SELECT COALESCE(SUM(pnl),0) AS pnl, COUNT(*) AS n FROM fills WHERE side='sell' AND ts>=?", (since,))
        closed = self.db.all("SELECT sol_in, sol_out FROM positions WHERE status='closed' AND closed_ts>=?", (since,))
        wins = sum(1 for p in closed if p["sol_out"] > p["sol_in"])
        unreal = sum((p["tokens_left"] / 10 ** p["decimals"]) * (p["last_price"] or 0) - p["sol_in"] * p["tokens_left"] / p["tokens_init"]
                     for p in self.open_positions())
        return {"realized": r["pnl"], "sells": r["n"], "closed": len(closed), "wins": wins, "unrealized": unreal}
