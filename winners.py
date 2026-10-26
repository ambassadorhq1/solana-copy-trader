"""Winners backfill scout: seed candidates from big recent Solana memecoin pumps (free Dexscreener + budgeted Helius).

1. Tokens (free, keyless Dexscreener): boosts + token profiles (as the normal scout), `/latest/dex/search` for a few
   query words, and mints the normal scout already found pumped in the last `max_age_days`. Kept: Solana pairs aged
   `min_age_h`..`max_age_days`, liquidity >= min_liquidity_usd, mcap >= min_mcap_usd. Ranked by mcap and by 24h change,
   interleaved; at most `tokens_per_run` new ones per run (each token is processed once).
2. Early buyers per token from the mint's oldest txs (same gTFA call the scout makes, `early_tx_limit` txs): bought within
   `early_window_min` of the first tx OR at <= `max_entry_frac_of_now` of today's price; the creator and launch-slot
   snipers are skipped. Stored in `winner_early` (and their swaps in `trades`, which also feeds bundle detection).
3. Only wallets that were early on >= `min_repeat` winners get a history fetch (the scorer's own backfill call,
   500 txs = 50 credits). From it they must have SOLD at >= `min_realized_mult` realized on >= `min_repeat` of those
   winners and have a swap in the last `active_days`. Survivors become candidates and go straight through the existing
   scorer (all rules + bundler / dev / sniper / insider / funder filters) -> watch / bench / rejected.
Budget: every Helius call is metered under purpose 'winners' (budget.winners_share of the daily allowance) and the
monthly hard stop applies. Runs daily at `run_hour` (scheduler) or on /scout_winners."""
import json

from .clients import BudgetExceeded, HeliusError
from .parse import fee_payer, parse_swaps
from .util import log, now, short

DAY = 86400
PURPOSE = "winners"


def wcfg(cfg):
    d = {"enabled": True, "run_hour": 3, "tokens_per_run": 20, "max_age_days": 30, "min_age_h": 24,
         "min_mcap_usd": 1_000_000, "min_liquidity_usd": 50_000, "search_queries": ["pump", "solana", "meme", "bonk"],
         "early_tx_limit": 200, "early_window_min": 60, "max_entry_frac_of_now": 0.2, "sniper_slots": 2,
         "max_buyers_per_token": 25, "min_repeat": 2, "min_realized_mult": 3.0, "min_sold_frac": 0.5,
         "active_days": 7, "wallets_per_run": 8, "recheck_days": 7}
    d.update(cfg.get("winners") or {})
    return d


def ensure_schema(db):
    f = "DOUBLE PRECISION" if db.pg else "REAL"
    db.x(f"CREATE TABLE IF NOT EXISTS winner_early (mint TEXT, wallet TEXT, ts BIGINT, sol {f}, px {f}, symbol TEXT, "
         f"added_at BIGINT, PRIMARY KEY (mint, wallet))")


# ---------------------------------------------------------------- 1) winner tokens (free Dexscreener)
async def _search(ctx, q):
    r = await ctx.dex.get(f"/latest/dex/search?q={q}")
    return [p for p in ((r or {}).get("pairs") or []) if p.get("chainId") == "solana"]


async def pick_winners(ctx):
    c, t, db = wcfg(ctx.cfg), now(), ctx.db
    mints = list(await ctx.dex.discover_mints())
    found = {}
    for q in c["search_queries"]:
        for p in await _search(ctx, q):
            m = (p.get("baseToken") or {}).get("address")
            if m and ((p.get("liquidity") or {}).get("usd") or 0) > ((found.get(m) or {}).get("liquidity") or {}).get("usd", -1):
                found[m] = p
    since = t - c["max_age_days"] * DAY
    mints += [r["mint"] for r in db.all("SELECT mint FROM tokens WHERE scouted_at>=?", (since,))]
    need = [m for m in dict.fromkeys(mints) if m not in found]
    found.update(await ctx.dex.best_pairs(need) if need else {})
    done = {r["mint"] for r in db.all("SELECT DISTINCT mint FROM winner_early")} | set((db.kv_get("winners_done") or {}).keys())
    keep = []
    for m, p in found.items():
        if m in done or (p.get("quoteToken") or {}).get("address") == m:
            continue
        created = (p.get("pairCreatedAt") or 0) / 1000
        age_h = (t - created) / 3600 if created else 1e9
        mcap = p.get("marketCap") or p.get("fdv") or 0
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if c["min_age_h"] <= age_h <= c["max_age_days"] * 24 and mcap >= c["min_mcap_usd"] and liq >= c["min_liquidity_usd"]:
            ch = (p.get("priceChange") or {}).get("h24") or 0
            keep.append((m, p, float(mcap), float(ch)))
    by_mcap = sorted(keep, key=lambda x: -x[2])
    by_gain = sorted(keep, key=lambda x: -x[3])
    out, seen = [], set()
    for a, b in zip(by_mcap, by_gain):  # interleave: largest mcap, top gainer, ...
        for x in (a, b):
            if x[0] not in seen:
                seen.add(x[0]); out.append(x)
    return out[: c["tokens_per_run"]]


# ---------------------------------------------------------------- 2) early buyers
def early_buyers(txs, mint, c, now_px=None):
    if not txs:
        return None, []
    txs = sorted(txs, key=lambda t: (t.get("slot") or 0, t.get("blockTime") or 0))
    first = txs[0]
    creator, s0, t0 = fee_payer(first), first.get("slot") or 0, first.get("blockTime") or 0
    seen, out = set(), []
    for tx in txs:
        if (tx.get("slot") or 0) < s0 + c["sniper_slots"]:
            continue
        for s in parse_swaps(tx):
            if s["mint"] != mint or s["side"] != "buy" or s["wallet"] == creator or s["wallet"] in seen or not s["tokens"]:
                continue
            px = s["sol"] / (s["tokens"] / 10 ** (s["decimals"] or 0))
            in_time = (s["ts"] or 0) <= t0 + c["early_window_min"] * 60
            cheap = bool(now_px) and px <= now_px * c["max_entry_frac_of_now"]
            if in_time or cheap:
                seen.add(s["wallet"]); out.append(dict(s, px=px))
    return {"creator": creator, "first_slot": s0, "first_ts": t0}, out[: c["max_buyers_per_token"]]


# ---------------------------------------------------------------- 3) repeaters -> realized >= 3x + still active -> scorer
def realized_mult(trades):
    bt = sum(r["tokens"] for r in trades if r["side"] == "buy")
    bs = sum(r["sol"] for r in trades if r["side"] == "buy")
    st = sum(r["tokens"] for r in trades if r["side"] == "sell")
    ss = sum(r["sol"] for r in trades if r["side"] == "sell")
    if not bt or not bs or not st:
        return None, 0.0
    sold = min(st / bt, 1.0)
    return ss * min(1.0, bt / st) / (bs * sold), sold


def repeaters(db, c):
    rows = db.all("SELECT wallet, COUNT(DISTINCT mint) AS n FROM winner_early GROUP BY wallet HAVING COUNT(DISTINCT mint)>=?",
                  (c["min_repeat"],))
    return [r["wallet"] for r in sorted(rows, key=lambda r: -r["n"])]


async def check_wallet(ctx, addr, c):
    """History fetch (scorer's backfill call) -> (passes, info). Stores swaps; marks the wallet backfilled."""
    from .scorer import store_swaps
    cfg, db, t = ctx.cfg, ctx.db, now()
    since = t - cfg["scoring"]["window_days"] * DAY
    txs = await ctx.helius.history(addr, PURPOSE, limit=cfg["scout"]["backfill_limit"], since=since, pages=cfg["scout"]["backfill_pages"])
    store_swaps(db, [s for tx in txs for s in parse_swaps(tx, owner=addr)])
    last = db.val("SELECT MAX(ts) AS m FROM trades WHERE wallet=? AND side IN ('buy','sell')", (addr,), 0) or 0
    wins = []
    for r in db.all("SELECT mint, symbol FROM winner_early WHERE wallet=?", (addr,)):
        tr = db.all("SELECT side, sol, tokens FROM trades WHERE wallet=? AND mint=? AND side IN ('buy','sell')", (addr, r["mint"]))
        m, sold = realized_mult(tr)
        if m is not None and m >= c["min_realized_mult"] and sold >= c["min_sold_frac"]:
            wins.append((r["symbol"] or short(r["mint"]), round(m, 2)))
    active = last >= t - c["active_days"] * DAY
    return active and len(wins) >= c["min_repeat"], {"active": active, "last_ts": last, "wins": wins}


async def run_winners(ctx, manual=False):
    c, db = wcfg(ctx.cfg), ctx.db
    res = {"tokens": 0, "early": 0, "repeaters": 0, "checked": 0, "passed": 0, "scored": {}, "stopped": None}
    if not c["enabled"] and not manual:
        res["stopped"] = "disabled"; return res
    ctx.cfg["budget"].setdefault("winners_share", 0.10)  # own daily guard even with an older config.yaml
    if ctx.budget.over_hard_stop():
        res["stopped"] = "monthly credit hard stop"; return res
    ensure_schema(db)
    done = db.kv_get("winners_done") or {}
    for m, p, mcap, ch in await pick_winners(ctx):
        now_px = await ctx.dex.price_sol(p)
        try:
            txs = await ctx.helius.history(m, PURPOSE, asc=True, limit=min(c["early_tx_limit"], 1000))
        except BudgetExceeded as e:
            res["stopped"] = f"budget: {e}"; break
        except HeliusError as e:
            log.warning("winners: history %s failed: %s", short(m), e)
            if e.code in (401, 403):
                break
            continue
        info, buyers = early_buyers(txs, m, c, now_px)
        sym = (p.get("baseToken") or {}).get("symbol")
        db.x("INSERT INTO tokens (mint, symbol, pair_created_at, creator, first_slot) VALUES (?,?,?,?,?) ON CONFLICT (mint) DO UPDATE SET "
             "creator=COALESCE(tokens.creator, excluded.creator), first_slot=COALESCE(tokens.first_slot, excluded.first_slot), "
             "symbol=COALESCE(tokens.symbol, excluded.symbol), pair_created_at=COALESCE(tokens.pair_created_at, excluded.pair_created_at)",
             (m, sym, int((p.get("pairCreatedAt") or 0) / 1000) or (info or {}).get("first_ts"), (info or {}).get("creator"),
              (info or {}).get("first_slot")))
        from .scorer import store_swaps
        store_swaps(db, [{k: b[k] for k in ("sig", "wallet", "mint", "side", "sol", "tokens", "decimals", "pre_tokens", "ts", "slot")}
                         for b in buyers])
        for b in buyers:
            db.x("INSERT INTO winner_early (mint, wallet, ts, sol, px, symbol, added_at) VALUES (?,?,?,?,?,?,?) "
                 "ON CONFLICT (mint, wallet) DO NOTHING", (m, b["wallet"], b["ts"], b["sol"], b["px"], sym, now()))
        done[m] = now()
        res["tokens"] += 1; res["early"] += len(buyers)
        log.info("winners %s mcap $%.0f: %d early buyers", sym, mcap, len(buyers))
    cutoff = now() - c["max_age_days"] * DAY * 2
    db.kv_set("winners_done", {k: v for k, v in done.items() if v >= cutoff})
    db.x("DELETE FROM winner_early WHERE added_at<?", (cutoff,))

    checked = db.kv_get("winners_checked") or {}
    reps = repeaters(db, c)
    res["repeaters"] = len(reps)
    from .scorer import score_wallet
    for addr in reps:
        if res["checked"] >= c["wallets_per_run"] or res["stopped"]:
            break
        w = db.one("SELECT * FROM wallets WHERE address=?", (addr,))
        if w and (w["status"] in ("watch", "qualified") or (w.get("scored_at") or 0) > now() - c["recheck_days"] * DAY):
            continue
        if (checked.get(addr) or 0) > now() - c["recheck_days"] * DAY:
            continue
        try:
            ok, info = await check_wallet(ctx, addr, c)
        except BudgetExceeded as e:
            res["stopped"] = f"budget: {e}"; break
        except HeliusError as e:
            log.warning("winners: wallet %s failed: %s", short(addr), e); continue
        res["checked"] += 1
        checked[addr] = now()
        if not ok:
            continue
        res["passed"] += 1
        src = "winners:" + ",".join(f"{s}@{m:g}x" for s, m in info["wins"][:4])
        db.x("INSERT INTO wallets (address, status, source, discovered_at) VALUES (?, 'candidate', ?, ?) ON CONFLICT (address) DO "
             "UPDATE SET source=excluded.source", (addr, src[:200], now()))
        db.x("UPDATE wallets SET backfilled_to=? WHERE address=?", (now(), addr))  # just fetched: the scorer only tops up
        try:
            _, status, s = await score_wallet(ctx, db.one("SELECT * FROM wallets WHERE address=?", (addr,)), PURPOSE)
            res["scored"][addr] = status
        except BudgetExceeded as e:
            res["stopped"] = f"budget: {e}"; break
        except Exception as e:
            log.exception("winners: score %s: %s", short(addr), e)
    db.kv_set("winners_checked", {k: v for k, v in checked.items() if v >= now() - c["recheck_days"] * DAY})
    if any(st == "watch" for st in res["scored"].values()):
        await ctx.watcher.sync()
    db.kv_set("last_winners", {"ts": now(), **{k: v for k, v in res.items() if k != "scored"},
                               "scored": len(res["scored"]), "watch": sum(st == "watch" for st in res["scored"].values())})
    db.event("winners", json.dumps({k: v for k, v in res.items() if k != "scored"}))
    return res


def format_result(res):
    from .tgbot import esc, table
    if res.get("stopped") in ("disabled", "monthly credit hard stop") and not res["tokens"]:
        return f"🏆 <b>Winners scout</b>\nSkipped: {esc(res['stopped'])}"
    st = list(res["scored"].values())
    rows = [["Winner tokens", str(res["tokens"])], ["Early buyers", str(res["early"])],
            ["Repeaters", str(res["repeaters"])], ["History checked", str(res["checked"])],
            ["≥3x + active", str(res["passed"])],
            ["→ watch", str(st.count("watch"))], ["→ bench", str(st.count("qualified"))],
            ["→ rejected", str(st.count("rejected"))]]
    out = "🏆 <b>Winners scout</b> · 📄 PAPER\n" + table(rows, "lr")
    if res["scored"]:
        out += esc("\n".join(f"{short(a)} → {s}" for a, s in list(res["scored"].items())[:10]))
    if res.get("stopped"):
        out += f"\n<i>Stopped early: {esc(res['stopped'])}</i>"
    return out
