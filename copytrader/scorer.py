"""Scorer: backfill a wallet's 30d swaps, compute stats, apply docs/architecture.md rules, promote / demote."""
import json

from .clients import BudgetExceeded
from .parse import parse_swaps, sol_funder
from .util import log, median, now, short

DAY = 86400


def compute_stats(trades, launch, cfg, t=None):
    c, t = cfg["scoring"], t or now()
    since = t - c["window_days"] * DAY
    by = {}
    for tr in trades:
        if tr["ts"] >= since:
            by.setdefault(tr["mint"], []).append(tr)
    closed, delays, sniper, insider = [], [], 0, 0
    for mint, rows in by.items():
        rows.sort(key=lambda r: r["ts"])
        buys = [r for r in rows if r["side"] == "buy"]
        sells = [r for r in rows if r["side"] == "sell"]
        if any(r["side"] == "in" for r in rows):
            insider += 1
        if buys and launch.get(mint):
            d = buys[0]["ts"] - launch[mint]
            if d >= 0:
                delays.append(d / 60)
                sniper += d <= c["sniper_seconds"]
        bt, st = sum(r["tokens"] for r in buys), sum(r["tokens"] for r in sells)
        if not buys or st < c["closed_sold_frac"] * bt:
            continue
        bsol = sum(r["sol"] for r in buys)
        ssol = sum(r["sol"] for r in sells) * min(1.0, bt / st)
        hold = sum(r["ts"] * r["tokens"] for r in sells) / st - buys[0]["ts"]
        closed.append({"mint": mint, "pnl": ssol - bsol, "mult": ssol / bsol if bsol else 0,
                       "hold": hold, "close_ts": sells[-1]["ts"]})
    n = len(closed)
    wins = [x for x in closed if x["mult"] >= 1 + c["win_min_gain"]]
    pnl = sum(x["pnl"] for x in closed)
    closed.sort(key=lambda x: x["close_ts"])
    streak = 0
    for x in reversed(closed):
        if x["mult"] >= 1:
            break
        streak += 1
    fast = c["fast_dump_seconds"]
    share = lambda k: (k / n) if n else 0.0
    return {
        "closed": n, "mints": len(by),
        "win_rate": round(share(len(wins)), 3),
        "pnl_sol": round(pnl, 3),
        "median_win_mult": round(median([w["mult"] for w in wins]) or 0, 2),
        "top_trade_share": round(max(x["pnl"] for x in closed) / pnl, 3) if pnl > 0 else 1.0,
        "median_entry_min": round(median(delays), 1) if delays else None,
        "sniper_share": round(sniper / len(delays), 3) if delays else 0.0,
        "median_hold_min": round(median([x["hold"] for x in closed]) / 60, 1) if closed else None,
        "insider_share": round(insider / len(by), 3) if by else 0.0,
        "fast_dump_share": round(share(sum(1 for x in closed if x["hold"] <= fast and x["mult"] >= 1.5)), 3),
        "wash_share": round(share(sum(1 for x in closed if x["hold"] <= fast and abs(x["mult"] - 1) < 0.03)), 3),
        "loss_streak": streak,
        "pnl_7d": round(sum(x["pnl"] for x in closed if x["close_ts"] >= t - 7 * DAY), 3),
    }


# scoring.profile: "strict" = the scoring block as written; "relaxed" (paper default) loosens only the performance rules
# for fresh wallets. Hard filters (sniper, dev/creator, bundler, funder cluster, wash, insider, fast-dump) never change.
PROFILES = {
    "strict": {},
    "relaxed": {"min_closed_trades": 3, "min_win_rate": 0.40, "min_realized_pnl_sol": 0.0, "min_median_win_mult": 1.5,
                "max_top_trade_share": 0.8, "max_median_entry_min": 240, "min_median_hold_min": 1,
                "max_median_hold_min": 10080, "allow_unknown_entry": True},
}
HARD_KEYS = ("sniper_seconds", "max_sniper_share", "max_bundle_share", "max_wash_share", "funder_cluster_max",
             "max_insider_share", "fast_dump_seconds", "max_fast_dump_share")


def rules(cfg):
    """Effective scoring thresholds: scoring block + profile overrides (config scoring.profiles.<name> beats PROFILES)."""
    c = dict(cfg["scoring"])
    name = str(c.get("profile") or "strict").lower()
    over = dict(PROFILES.get(name, {}))
    over.update(((c.get("profiles") or {}).get(name)) or {})
    for k, v in over.items():
        if k not in HARD_KEYS:
            c[k] = v
    c["profile"] = name if name in PROFILES else "strict"
    return c


def evaluate(s, cfg):
    c, r = rules(cfg), []
    if s["closed"] < c["min_closed_trades"]: r.append(f"closed {s['closed']}<{c['min_closed_trades']}")
    if s["win_rate"] < c["min_win_rate"]: r.append(f"winrate {s['win_rate']:.0%}")
    if s["pnl_sol"] < c["min_realized_pnl_sol"] or s["pnl_sol"] <= 0: r.append(f"pnl {s['pnl_sol']:.1f}")
    if s["median_win_mult"] < c["min_median_win_mult"]: r.append(f"med win {s['median_win_mult']}x")
    if s["top_trade_share"] >= c["max_top_trade_share"]: r.append(f"one-trade {s['top_trade_share']:.0%}")
    me = s["median_entry_min"]
    if (me is None and not c.get("allow_unknown_entry")) or (me is not None and not (c["min_median_entry_min"] <= me <= c["max_median_entry_min"])):
        r.append(f"entry {me}m")
    if s["sniper_share"] > c["max_sniper_share"]: r.append("sniper")
    mh = s["median_hold_min"]
    if mh is None or not (c["min_median_hold_min"] <= mh <= c["max_median_hold_min"]): r.append(f"hold {mh}m")
    if s["insider_share"] > c["max_insider_share"]: r.append("insider")
    if s["fast_dump_share"] > c["max_fast_dump_share"]: r.append("fast-dump")
    if s["wash_share"] > c["max_wash_share"]: r.append("wash")
    if s.get("bundle_share", 0) > c["max_bundle_share"]: r.append("bundler")
    return r


def composite(s):
    return round(40 * s["win_rate"] + 30 * min(max(s["pnl_sol"], 0) / 200, 1)
                 + 20 * min(s["median_win_mult"] / 5, 1) + 10 * (1 - min(s["top_trade_share"], 1)), 2)


def store_swaps(db, swaps):
    new = []
    for s in swaps:
        cur = db.x("INSERT INTO trades (sig, wallet, mint, side, sol, tokens, decimals, pre_tokens, ts, slot) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                   (s["sig"], s["wallet"], s["mint"], s["side"], s["sol"], s["tokens"], s["decimals"],
                    s["pre_tokens"], s["ts"], s["slot"]))
        if cur.rowcount and cur.rowcount > 0:
            new.append(s)
    return new


async def launch_times(ctx, mints):
    rows = {}
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        q = "SELECT mint, pair_created_at FROM tokens WHERE mint IN (%s)" % ",".join("?" * len(chunk))
        rows.update({r["mint"]: r["pair_created_at"] for r in ctx.db.all(q, chunk)})
    missing = [m for m in mints if m not in rows][:90]  # <= 3 Dexscreener calls
    if missing:
        for m, p in (await ctx.dex.best_pairs(missing)).items():
            ts = int((p.get("pairCreatedAt") or 0) / 1000) or None
            rows[m] = ts
            ctx.db.x("INSERT INTO tokens (mint, symbol, pair_created_at) VALUES (?,?,?) ON CONFLICT (mint) DO NOTHING",
                     (m, (p.get("baseToken") or {}).get("symbol"), ts))
    return rows


async def score_wallet(ctx, w, purpose="scout"):
    cfg, db, t = ctx.cfg, ctx.db, now()
    c = cfg["scoring"]
    since = max(w.get("backfilled_to") or 0, t - c["window_days"] * DAY)
    txs = await ctx.helius.history(w["address"], purpose, limit=cfg["scout"]["backfill_limit"],
                                   since=since, pages=cfg["scout"]["backfill_pages"])
    store_swaps(db, [s for tx in txs for s in parse_swaps(tx, owner=w["address"])])
    trades = db.all("SELECT * FROM trades WHERE wallet=? AND ts>=?", (w["address"], t - c["window_days"] * DAY))
    s = compute_stats(trades, await launch_times(ctx, list({x["mint"] for x in trades})), cfg, t)
    nb = db.val("SELECT COUNT(*) AS n FROM trades WHERE wallet=? AND side='buy' AND ts>=?", (w["address"], t - c["window_days"] * DAY), 0)
    bundled = db.val("SELECT COUNT(*) AS n FROM trades a WHERE a.wallet=? AND a.side='buy' AND a.ts>=? AND EXISTS "
                     "(SELECT 1 FROM trades b WHERE b.mint=a.mint AND b.slot=a.slot AND b.side='buy' AND b.wallet<>a.wallet)",
                     (w["address"], t - c["window_days"] * DAY), 0)
    s["bundle_share"] = round(bundled / nb, 3) if nb else 0.0
    reasons = evaluate(s, cfg)
    if db.one("SELECT 1 AS x FROM tokens WHERE creator=?", (w["address"],)):
        reasons.append("dev/creator")
    from .deep import insider_override  # 🎯 insider_likely: volume rules waived, hard filters kept, ranked first
    ins = insider_override(db, w["address"], reasons, cfg, t)
    if ins:
        reasons = ins[0]
    funder = w.get("funder")
    if not reasons and not funder:
        first = await ctx.helius.history(w["address"], purpose, asc=True, limit=10)
        funder = next((f for f in (sol_funder(tx, w["address"]) for tx in first) if f), None) or "-"
    if funder and funder != "-" and not reasons:
        if db.one("SELECT 1 AS x FROM tokens WHERE creator=?", (funder,)):
            reasons.append("funded by deployer")
        n_same = db.val("SELECT COUNT(*) AS n FROM wallets WHERE funder=? AND address<>?", (funder, w["address"]), 0)
        if n_same + 1 >= c["funder_cluster_max"]:
            reasons.append(f"funder cluster x{n_same + 1}")
    score = composite(s) + (ins[1] if ins else 0)
    old = w.get("status")
    if ins and ins[2]:
        status = "dormant"  # no swap in the last 7 days: off the watchlist, re-checked weekly by the deep scout
    elif old == "watch":
        demote = s["loss_streak"] >= c["demote_loss_streak"] or s["pnl_7d"] < 0 or (c["demote_on_rule_fail"] and reasons)
        status = "demoted" if demote else "watch"
    else:
        n_watch = db.val("SELECT COUNT(*) AS n FROM wallets WHERE status='watch'", (), 0)
        status = ("watch" if n_watch < c["max_watch"] else "qualified") if not reasons else "rejected"
    reason = ", ".join(reasons) or ("inactive >7d" if status == "dormant" else
                                    "loss streak" if s["loss_streak"] >= c["demote_loss_streak"] else
                                    "7d pnl<0" if status == "demoted" else "insider_likely" if ins else "ok")
    db.x("UPDATE wallets SET status=?, score=?, stats=?, reason=?, funder=?, scored_at=?, backfilled_to=? WHERE address=?",
         (status, score, json.dumps(s), reason, funder, t, t, w["address"]))
    log.info("score %s -> %s (%s) %s", short(w["address"]), status, score, reason)
    return old, status, s


async def process_candidates(ctx, limit):
    changed, n = [], 0
    for w in ctx.db.all("SELECT * FROM wallets WHERE status='candidate' ORDER BY discovered_at LIMIT ?", (limit,)):
        try:
            old, new, s = await score_wallet(ctx, w, "scout")
            n += 1
            if new == "watch":
                changed.append((w["address"], s))
        except BudgetExceeded as e:
            log.info("backfill budget reached: %s", e); break
        except Exception as e:
            log.exception("score %s failed: %s", w["address"], e)
    if changed:
        await ctx.watcher.sync()
        for a, s in changed:
            from .deep import is_insider
            await ctx.tg.send(f"➕ {'🎯 ' if is_insider(ctx.db, a) else ''}Watching {short(a)}: {s['closed']} trades, WR {s['win_rate']:.0%}, "
                              f"PnL {s['pnl_sol']:.1f} SOL, med win {s['median_win_mult']}x")
    return n


async def daily_rescore(ctx):
    demoted = []
    for w in ctx.db.all("SELECT * FROM wallets WHERE status='watch'"):
        try:
            old, new, s = await score_wallet(ctx, w, "score")
            if new == "demoted":
                demoted.append(short(w["address"]))
        except BudgetExceeded:
            break
        except Exception as e:
            log.exception("rescore %s: %s", w["address"], e)
    slots = ctx.cfg["scoring"]["max_watch"] - ctx.db.val("SELECT COUNT(*) AS n FROM wallets WHERE status='watch'", (), 0)
    promoted = []
    for w in ctx.db.all("SELECT address FROM wallets WHERE status='qualified' ORDER BY score DESC LIMIT ?", (max(slots, 0),)):
        ctx.db.x("UPDATE wallets SET status='watch' WHERE address=?", (w["address"],)); promoted.append(short(w["address"]))
    await ctx.watcher.sync()
    if demoted or promoted:
        await ctx.tg.send(f"🔁 Daily rescore: demoted {', '.join(demoted) or 'none'}; promoted {', '.join(promoted) or 'none'}")
    return demoted, promoted
