"""Smart-money cluster alerts: several WATCHLIST wallets buying the same NEW token -> one Telegram alert.

Input: the swaps the watcher already stores in `trades` (no extra Helius credits). Called after each new buy signal.
Trigger (all configurable under `clusters:` in config.yaml), counted over buys in [buy_ts - window, buy_ts]:
  * >= min_wallets distinct watch wallets, OR
  * >= min_wallets_for_amount wallets whose combined buys are >= min_total_sol.
The token must be younger than max_token_age_h (pair age from the `tokens` table, else one keyless Dexscreener
lookup, made only once the wallet thresholds already pass, cached in memory for 10 minutes).
Dedupe: one alert per token, then again only when the wallet count grows by >= realert_wallets_delta or the total SOL
reaches realert_sol_mult x the last alerted total. Alert only: it never buys (`auto_paper_buy` is reserved for later).
"""
import json, time

from .util import log, now, short

DEX_URL = "https://dexscreener.com/solana/"


def ccfg(cfg):
    d = {"enabled": True, "min_wallets": 3, "window_min": 60, "min_total_sol": 5.0, "min_wallets_for_amount": 2,
         "max_token_age_h": 24, "realert_wallets_delta": 2, "realert_sol_mult": 2.0, "auto_paper_buy": False}
    d.update(cfg.get("clusters") or {})
    return d


def ensure_schema(db):
    f = "DOUBLE PRECISION" if db.pg else "REAL"
    db.x(f"CREATE TABLE IF NOT EXISTS clusters (mint TEXT PRIMARY KEY, symbol TEXT, first_ts BIGINT, last_ts BIGINT, "
         f"alert_ts BIGINT, alerts INTEGER DEFAULT 0, wallets INTEGER, total_sol {f}, alert_wallets INTEGER, "
         f"alert_sol {f}, pair_created_at BIGINT, liq_usd {f}, mcap_usd {f}, members TEXT)")


_pair_cache = {}  # mint -> (fetched_at, pair or None)


async def _pair(ctx, mint):
    hit = _pair_cache.get(mint)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        p = (await ctx.dex.best_pairs([mint])).get(mint)
    except Exception as e:
        log.warning("cluster pair lookup %s: %s", short(mint), e); p = None
    _pair_cache[mint] = (time.time(), p)
    if len(_pair_cache) > 500:
        for k in sorted(_pair_cache, key=lambda k: _pair_cache[k][0])[:250]:
            _pair_cache.pop(k, None)
    return p


def members(db, mint, t0, t1):
    rows = db.all("SELECT t.wallet AS wallet, SUM(t.sol) AS sol, MIN(t.ts) AS first_ts, MAX(w.score) AS score "
                  "FROM trades t JOIN wallets w ON w.address=t.wallet "
                  "WHERE t.mint=? AND t.side='buy' AND w.status='watch' AND t.ts>=? AND t.ts<=? "
                  "GROUP BY t.wallet ORDER BY first_ts", (mint, t0, t1))
    return [{"wallet": r["wallet"], "sol": float(r["sol"] or 0), "ts": int(r["first_ts"]), "score": float(r["score"] or 0)}
            for r in rows]


def triggered(c, n, total):
    return n >= c["min_wallets"] or (n >= c["min_wallets_for_amount"] and total >= c["min_total_sol"] - 1e-9)


async def on_buy(ctx, s):
    """Check one freshly stored buy. Returns the alert text when an alert was sent (for tests), else None."""
    c, db = ccfg(ctx.cfg), ctx.db
    if not c["enabled"] or s.get("side") != "buy":
        return None
    t1 = s.get("ts") or now()
    win = c["window_min"] * 60
    if now() - t1 > win:
        return None  # an old swap (backfill / late poll) is not a live cluster
    w = db.one("SELECT status FROM wallets WHERE address=?", (s["wallet"],))
    if not w or w["status"] != "watch":
        return None
    mint = s["mint"]
    ms = members(db, mint, t1 - win, t1)
    n, total = len(ms), sum(m["sol"] for m in ms)
    if not triggered(c, n, total):
        return None
    prev = db.one("SELECT * FROM clusters WHERE mint=?", (mint,))
    tok = db.one("SELECT symbol, pair_created_at FROM tokens WHERE mint=?", (mint,)) or {}
    pair = await _pair(ctx, mint)  # free + keyless, only reached once the wallet thresholds pass
    created = int(((pair or {}).get("pairCreatedAt") or 0) / 1000) or tok.get("pair_created_at") or (prev or {}).get("pair_created_at")
    if not created:
        log.info("cluster %s: token age unknown, skipped", short(mint)); return None
    if now() - created > c["max_token_age_h"] * 3600:
        return None
    sym = ((pair or {}).get("baseToken") or {}).get("symbol") or tok.get("symbol") or (prev or {}).get("symbol")
    liq = ((pair or {}).get("liquidity") or {}).get("usd") or (prev or {}).get("liq_usd")
    mcap = (pair or {}).get("marketCap") or (pair or {}).get("fdv") or (prev or {}).get("mcap_usd")
    mem = json.dumps([{k: m[k] for k in ("wallet", "sol", "ts", "score")} for m in ms])
    if prev and prev.get("alert_wallets"):
        grew = n >= prev["alert_wallets"] + c["realert_wallets_delta"] or total >= (prev["alert_sol"] or 0) * c["realert_sol_mult"] - 1e-9
        if not grew:
            db.x("UPDATE clusters SET last_ts=?, wallets=?, total_sol=?, members=?, liq_usd=?, mcap_usd=? WHERE mint=?",
                 (t1, n, total, mem, liq, mcap, mint))
            return None
    text = format_alert(mint, sym, created, liq, mcap, ms, total, prev, db)
    if prev:
        db.x("UPDATE clusters SET symbol=?, last_ts=?, alert_ts=?, alerts=alerts+1, wallets=?, total_sol=?, alert_wallets=?, "
             "alert_sol=?, pair_created_at=?, liq_usd=?, mcap_usd=?, members=? WHERE mint=?",
             (sym, t1, now(), n, total, n, total, created, liq, mcap, mem, mint))
    else:
        db.x("INSERT INTO clusters (mint, symbol, first_ts, last_ts, alert_ts, alerts, wallets, total_sol, alert_wallets, alert_sol, "
             "pair_created_at, liq_usd, mcap_usd, members) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?)",
             (mint, sym, ms[0]["ts"], t1, now(), n, total, n, total, created, liq, mcap, mem))
    db.event("cluster", f"{sym or short(mint)} {n} wallets {total:.3f} SOL")
    await ctx.tg.send(text)
    if c["auto_paper_buy"]:
        log.info("clusters.auto_paper_buy is reserved for later; no buy was made")
    return text


def usd(x):
    if not x:
        return "-"
    x = float(x)
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.1f}k" if x >= 1e3 else f"${x:.0f}"


def format_alert(mint, sym, created, liq, mcap, ms, total, prev=None, db=None):
    from .tgbot import age, esc, table, tok
    head = "🧠 <b>Smart-money cluster</b>" if not prev else (
        f"🧠 <b>Cluster growing</b> · was {prev['alert_wallets']} · {prev['alert_sol'] or 0:.3f} SOL")
    span = max(m["ts"] for m in ms) - min(m["ts"] for m in ms)
    from .deep import insider_set
    ins = insider_set(db) if db is not None else set()
    t = [["WALLET", "SC", "SOL", ""]] + [[short(m["wallet"], 3), f"{m['score']:.0f}", f"{m['sol']:.3f}", "🎯" if m["wallet"] in ins else ""]
                                         for m in ms[:15]]
    n_ins = sum(m["wallet"] in ins for m in ms)
    more = f"\n+{len(ms) - 15} more" if len(ms) > 15 else ""
    return (f"{head} · <b>{esc(tok(sym, mint, 10))}</b>\n"
            f"<code>{esc(short(mint))}</code> · age {age(created)} · liq {usd(liq)} · mcap {usd(mcap)}\n"
            f"{len(ms)} watch wallets · {total:.3f} SOL in {span // 60}m" + (f" · 🎯 {n_ins} insider-likely" if n_ins else "") + "\n"
            + table(t) + esc(more)
            + f'<a href="{esc(DEX_URL + mint)}">Dexscreener</a> · 📄 alert only, no auto-buy')


def recent(db, limit=10):
    rows = db.all("SELECT * FROM clusters ORDER BY alert_ts DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        try:
            ms = json.loads(r.get("members") or "[]")
        except ValueError:
            ms = []
        out.append({"mint": r["mint"], "mint_short": short(r["mint"]), "symbol": r.get("symbol") or short(r["mint"]),
                    "wallets": r["wallets"], "total_sol": float(r["total_sol"] or 0), "alerts": r["alerts"],
                    "first_ts": r["first_ts"], "last_ts": r["last_ts"], "alert_ts": r["alert_ts"],
                    "pair_created_at": r["pair_created_at"], "liq_usd": r["liq_usd"], "mcap_usd": r["mcap_usd"],
                    "dex_url": DEX_URL + r["mint"],
                    "members": [{"wallet": short(m["wallet"]), "score": m.get("score"), "sol": m.get("sol")} for m in ms]})
    return out
