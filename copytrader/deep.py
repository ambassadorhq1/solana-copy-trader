"""Deep winners scout (1-year lookback, slow): weekly + `/scout_winners deep`. Also the `insider_likely` tag (🎯).

Tokens
  * `scout.seed_winners` (curated mints, looked up on Dexscreener) + Dexscreener's free search / boosts / profiles,
    filtered to Solana, launched <= `max_age_days` ago (seeds: `seed_max_age_days`), mcap >= min_mcap_usd, 24h volume >=
    min_volume_usd, and liquidity/mcap < max_liq_mcap_ratio (clone tokens with fake liquidity == mcap are dropped).
  * Launch time = the mint's first tx (gTFA asc, limit 10 = 10 credits), else Dexscreener's earliest pair.

Early buyers without paging through millions of signatures
  * Mode A (gTFA works): `getTransactionsForAddress` sortOrder=asc with a blockTime window. We sample slices that start
    at launch + `slices_min` and end at launch + `window_min` (`slice_limit` txs each, 10 credits per 100). A huge token
    therefore costs ~80 credits, whatever its total history.
  * Mode B (gTFA refused on the plan): standard `getSignaturesForAddress` on the earliest pool / bonding curve has no
    oldest-first option, so we page back from the newest only while the projected page count stays under
    `max_sig_pages`; tokens with more history are skipped and logged (too expensive). Then `max_fallback_txs` sampled
    `getTransaction` calls inside the window. Both are metered at `archival_cost` credits (Helius docs disagree: 1 vs 10).
  * Per-token cap `max_credits_per_token`, per-run cap `max_credits_per_run`, plus the budget guard
    (purpose 'deep', budget.deep_share of the day) and the monthly hard stop.

Who counts (still excluded: the dev/creator, block-0/1 snipers (`sniper_slots`), and same-slot bundles of
>= `bundle_min_wallets` buyers). `pre_move` = bought at <= `pre_move_frac` x the higher of today's price and the highest
price seen in the window (the token later ran >= 1/pre_move_frac x from their entry).
Wallets pre_move on >= `min_repeat` separate winners (deep + 30-day winners scout) get one cheap activity probe:
  * swap / tx in the last `active_days` -> tag insider_likely 🎯, candidate -> existing scorer, where the volume rules
    (trade count, win rate, PnL, median win, one-trade share, entry delay, hold time) are waived but every hard filter
    (sniper, insider transfers, bundler, dev, deployer-funded, funder cluster, wash, fast-dump) still applies; they get
    `insider_score_bonus` so they rank first.
  * otherwise -> status 'dormant' (tag dormant), re-probed weekly; once active again they go through the scorer.
"""
import json, math, time

from .clients import BudgetExceeded, HeliusError
from .parse import fee_payer, parse_swaps
from .util import is_excluded, log, now, short

DAY = 86400
PURPOSE = "deep"
HARD = ("sniper", "insider", "fast-dump", "wash", "bundler", "dev/creator", "funded by deployer", "funder cluster")


def dcfg(cfg):
    d = {"enabled": True, "run_weekday": 6, "run_hour": 4, "max_age_days": 365, "seed_max_age_days": 365,
         "tokens_per_run": 12, "min_mcap_usd": 10_000_000, "min_volume_usd": 50_000, "max_liq_mcap_ratio": 0.6,
         "search_queries": ["pump", "meme", "solana", "dog", "cat", "ai", "official", "coin"],
         "window_min": 60, "slices_min": [0, 2, 5, 10, 20, 35, 50], "slice_limit": 100, "sniper_slots": 2,
         "bundle_min_wallets": 3, "pre_move_frac": 0.25, "max_buyers_per_token": 60,
         "max_credits_per_token": 400, "max_credits_per_run": 12000, "archival_cost": 10,
         "max_sig_pages": 10, "max_fallback_txs": 30,
         "min_repeat": 2, "active_days": 7, "wallets_per_run": 10, "dormant_recheck_days": 7, "dormant_per_run": 20,
         "insider_score_bonus": 100}
    d.update(((cfg.get("scout") or {}).get("deep")) or {})
    return d


def seeds(cfg):
    return [s for s in ((cfg.get("scout") or {}).get("seed_winners") or []) if isinstance(s, dict) and s.get("mint")]


def ensure_schema(db):
    f = "DOUBLE PRECISION" if db.pg else "REAL"
    db.x(f"CREATE TABLE IF NOT EXISTS deep_early (mint TEXT, wallet TEXT, ts BIGINT, slot BIGINT, sol {f}, px {f}, "
         f"pre_move INTEGER, bundled INTEGER, symbol TEXT, added_at BIGINT, PRIMARY KEY (mint, wallet))")
    db.x("CREATE TABLE IF NOT EXISTS wallet_tags (address TEXT PRIMARY KEY, tag TEXT, winners INTEGER, mints TEXT, "
         "last_active BIGINT, checked_at BIGINT, updated_at BIGINT)")


# ---------------------------------------------------------------- tags (used by scorer, Telegram, dashboard, alerts)
def insider_set(db):
    try:
        return {r["address"] for r in db.all("SELECT address FROM wallet_tags WHERE tag='insider_likely'")}
    except Exception:
        return set()


def is_insider(db, address):
    try:
        return bool(db.one("SELECT 1 AS x FROM wallet_tags WHERE address=? AND tag='insider_likely'", (address,)))
    except Exception:
        return False


def set_tag(db, address, tag, winners=None, mints=None, last_active=None):
    db.x("INSERT INTO wallet_tags (address, tag, winners, mints, last_active, checked_at, updated_at) VALUES (?,?,?,?,?,?,?) "
         "ON CONFLICT (address) DO UPDATE SET tag=excluded.tag, winners=COALESCE(excluded.winners, wallet_tags.winners), "
         "mints=COALESCE(excluded.mints, wallet_tags.mints), last_active=COALESCE(excluded.last_active, wallet_tags.last_active), "
         "checked_at=excluded.checked_at, updated_at=excluded.updated_at",
         (address, tag, winners, json.dumps(mints) if mints is not None else None, last_active, now(), now()))


def insider_override(db, address, reasons, cfg, t=None):
    """Called by the scorer. None unless the wallet is tagged insider_likely; else (hard_reasons, bonus, inactive)."""
    if not is_insider(db, address):
        return None
    c, t = dcfg(cfg), t or now()
    hard = [r for r in reasons if r.startswith(HARD)]
    last = db.val("SELECT MAX(ts) AS m FROM trades WHERE wallet=?", (address,), 0) or 0
    row = db.one("SELECT last_active FROM wallet_tags WHERE address=?", (address,)) or {}
    last = max(last, row.get("last_active") or 0)
    inactive = last < t - c["active_days"] * DAY
    if inactive:
        set_tag(db, address, "dormant", last_active=last)
    return hard, (0 if inactive else c["insider_score_bonus"]), inactive


# ---------------------------------------------------------------- tokens
async def _pairs_of(ctx, mint):
    r = await ctx.dex.get(f"/token-pairs/v1/solana/{mint}")
    return [p for p in (r or []) if isinstance(p, dict) and p.get("chainId", "solana") == "solana"]


def _fake(p, c):
    liq = (p.get("liquidity") or {}).get("usd") or 0
    mcap = p.get("marketCap") or p.get("fdv") or 0
    return bool(mcap) and liq / mcap > c["max_liq_mcap_ratio"]


async def pick_tokens(ctx, done):
    c, t = dcfg(ctx.cfg), now()
    out, seen = [], set(done)
    for s in seeds(ctx.cfg):  # curated first
        m = s["mint"]
        if m in seen:
            continue
        seen.add(m)
        out.append({"mint": m, "symbol": s.get("symbol"), "seed": True})
    found = {}
    for q in c["search_queries"]:
        r = await ctx.dex.get(f"/latest/dex/search?q={q}")
        for p in ((r or {}).get("pairs") or []):
            m = (p.get("baseToken") or {}).get("address")
            if p.get("chainId") != "solana" or not m or m in seen:
                continue
            a = found.setdefault(m, {"p": p, "vol": 0.0})
            a["vol"] += (p.get("volume") or {}).get("h24") or 0
            if ((p.get("liquidity") or {}).get("usd") or 0) > ((a["p"].get("liquidity") or {}).get("usd") or 0):
                a["p"] = p
    for m in await ctx.dex.discover_mints():
        if m not in seen and m not in found:
            found[m] = {"p": None, "vol": None}
    need = [m for m, a in found.items() if a["p"] is None]
    for m, p in (await ctx.dex.best_pairs(need) if need else {}).items():
        found[m] = {"p": p, "vol": (p.get("volume") or {}).get("h24") or 0}
    disc = []
    for m, a in found.items():
        p = a["p"]
        if not p or _fake(p, c) or is_excluded(m, (p.get("baseToken") or {}).get("symbol")):
            continue
        created = (p.get("pairCreatedAt") or 0) / 1000
        mcap = p.get("marketCap") or p.get("fdv") or 0
        if created and t - created <= c["max_age_days"] * DAY and mcap >= c["min_mcap_usd"] and (a["vol"] or 0) >= c["min_volume_usd"]:
            disc.append((mcap, {"mint": m, "symbol": (p.get("baseToken") or {}).get("symbol"), "seed": False}))
    disc.sort(key=lambda x: -x[0])
    return (out + [d for _, d in disc])[: c["tokens_per_run"]]


# ---------------------------------------------------------------- early-buyer extraction
def _refused(e):
    m = str(e).lower()
    return getattr(e, "code", None) in (-32601, -32403, 403) or any(w in m for w in ("not available", "not supported", "upgrade", "plan"))


class TokenBudget:
    def __init__(self, ctx, cap_token, run_left):
        self.ctx, self.cap = ctx, min(cap_token, run_left)
        self.start = ctx.budget.used(purpose=PURPOSE)

    def spent(self):
        return self.ctx.budget.used(purpose=PURPOSE) - self.start

    def ok(self, est):
        return self.spent() + est <= self.cap


async def _mode_a(ctx, mint, pair_addr, t0, c, tb):
    """gTFA asc slices inside [t0, t0 + window]."""
    h, end = ctx.helius, t0 + c["window_min"] * 60
    est = 10 * math.ceil(c["slice_limit"] / 100)
    txs, seen = [], set()
    for off in c["slices_min"]:
        since = t0 + off * 60
        if since >= end:
            continue
        for addr in [mint] + ([pair_addr] if pair_addr and pair_addr != mint else []):
            if not tb.ok(est):
                return txs, "per-token credit cap"
            got = await h.gtfa(addr, PURPOSE, asc=True, limit=c["slice_limit"], since=since, until=end)
            for tx in got:
                sig = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
                if sig not in seen:
                    seen.add(sig); txs.append(tx)
            if got:
                break  # the mint answered; the pool is only a fallback for venues that don't reference the mint
    return txs, None


async def _mode_b(ctx, pair_addr, t0, c, tb):
    """No gTFA: page signatures back from the newest on the earliest pool / bonding curve, only if it's cheap."""
    h, end, cost = ctx.helius, t0 + c["window_min"] * 60, c["archival_cost"]
    if not pair_addr:
        return [], "no pool address for the signature fallback"
    sigs, before = [], None
    for page in range(c["max_sig_pages"]):
        if not tb.ok(cost):
            return [], "per-token credit cap"
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        got = [s for s in (await h.rpc("getSignaturesForAddress", [pair_addr, opts], cost, PURPOSE) or []) if not s.get("err")]
        sigs += got
        if not got or (got[-1].get("blockTime") or 0) <= t0 or len(got) < 1000:
            break
        span = (got[0].get("blockTime") or now()) - (got[-1].get("blockTime") or 0)
        left = (got[-1].get("blockTime") or 0) - t0
        if span > 0 and page + 1 + left / span > c["max_sig_pages"]:
            return [], f"too expensive: ~{int(left / span) + page + 1} signature pages back to launch (cap {c['max_sig_pages']})"
        before = got[-1]["signature"]
    else:
        return [], f"too expensive: launch not reached within {c['max_sig_pages']} signature pages"
    inwin = sorted((s for s in sigs if t0 <= (s.get("blockTime") or 0) <= end), key=lambda s: s.get("blockTime") or 0)
    if len(inwin) > c["max_fallback_txs"]:
        step = len(inwin) / c["max_fallback_txs"]
        inwin = [inwin[int(i * step)] for i in range(c["max_fallback_txs"])]
    txs = []
    for s in inwin:
        if not tb.ok(cost):
            break
        tx = await h.rpc("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}], cost, PURPOSE)
        if tx:
            txs.append(tx)
    return txs, None


def extract(txs, mint, creator, c, now_px=None):
    """-> early buys with pre_move / bundled flags (creator + block-0/1 snipers removed)."""
    buys = []
    for tx in sorted(txs, key=lambda t: (t.get("slot") or 0, t.get("blockTime") or 0)):
        for s in parse_swaps(tx):
            if s["mint"] == mint and s["side"] == "buy" and s["tokens"] and s["wallet"] != creator:
                buys.append(dict(s, px=s["sol"] / (s["tokens"] / 10 ** (s["decimals"] or 0))))
    if not buys:
        return []
    first_slot = min(b["slot"] or 0 for b in buys)
    if txs:
        first_slot = min(first_slot, min(t.get("slot") or first_slot for t in txs))
    buys = [b for b in buys if (b["slot"] or 0) >= first_slot + c["sniper_slots"]]
    per_slot = {}
    for b in buys:
        per_slot.setdefault(b["slot"], set()).add(b["wallet"])
    ref = max([now_px or 0] + [b["px"] for b in buys])
    out, seen = [], set()
    for b in buys:
        if b["wallet"] in seen:
            continue
        seen.add(b["wallet"])
        b["bundled"] = int(len(per_slot.get(b["slot"], ())) >= c["bundle_min_wallets"])
        b["pre_move"] = int(b["px"] <= ref * c["pre_move_frac"])
        out.append(b)
    return out[: c["max_buyers_per_token"]]


async def scan_token(ctx, tk, c, run_left):
    """-> (n_early, skip_reason, credits, mode)"""
    from .scorer import store_swaps
    db, mint, t = ctx.db, tk["mint"], now()
    pairs = await _pairs_of(ctx, mint)
    first = min((p for p in pairs if p.get("pairCreatedAt")), key=lambda p: p["pairCreatedAt"], default=None)
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, default=None)
    sym = tk.get("symbol") or ((best or first or {}).get("baseToken") or {}).get("symbol") or short(mint)
    created = int((first or {}).get("pairCreatedAt", 0) / 1000) or None
    max_age = c["seed_max_age_days"] if tk.get("seed") else c["max_age_days"]
    if created and t - created > max_age * DAY:
        return sym, 0, f"launched {(t - created) // DAY}d ago (> {max_age}d lookback)", 0, None
    tb = TokenBudget(ctx, c["max_credits_per_token"], run_left)
    creator, t0, mode, why = None, created, None, None
    try:
        if ctx.helius.gtfa_ok:
            try:
                probe = await ctx.helius.gtfa(mint, PURPOSE, asc=True, limit=10)
                if probe:
                    creator, t0 = fee_payer(probe[0]), min(x for x in [probe[0].get("blockTime"), created] if x)
                mode = "gtfa"
            except HeliusError as e:
                if not _refused(e):
                    raise
                log.warning("deep: getTransactionsForAddress refused (%s); signature fallback", e)
                ctx.helius.gtfa_ok = False
        if not t0:
            return sym, 0, "launch time unknown", tb.spent(), mode
        if t - t0 > max_age * DAY:
            return sym, 0, f"launched {(t - t0) // DAY}d ago (> {max_age}d lookback)", tb.spent(), mode
        if mode == "gtfa":
            txs, why = await _mode_a(ctx, mint, (first or {}).get("pairAddress"), t0, c, tb)
        else:
            mode = "signatures"
            txs, why = await _mode_b(ctx, (first or {}).get("pairAddress"), t0, c, tb)
    except BudgetExceeded as e:
        return sym, 0, f"budget guard: {e}", tb.spent(), mode
    now_px = await ctx.dex.price_sol(best) if best else None
    early = extract(txs, mint, creator, c, now_px)
    if not early:  # always say why (TRUMP used to be skipped with reason None)
        why = why or (f"no transactions in the {c['window_min']}m launch window ({mode or 'no mode'})" if not txs else
                      f"{len(txs)} launch-window txs but no parsable early buys (creator / block-0-1 snipers removed)")
        return sym, 0, why, tb.spent(), mode
    db.x("INSERT INTO tokens (mint, symbol, pair_created_at, creator) VALUES (?,?,?,?) ON CONFLICT (mint) DO UPDATE SET "
         "creator=COALESCE(tokens.creator, excluded.creator), symbol=COALESCE(tokens.symbol, excluded.symbol), "
         "pair_created_at=COALESCE(tokens.pair_created_at, excluded.pair_created_at)", (mint, sym, t0, creator))
    store_swaps(db, [{k: b[k] for k in ("sig", "wallet", "mint", "side", "sol", "tokens", "decimals", "pre_tokens", "ts", "slot")}
                     for b in early])
    for b in early:
        db.x("INSERT INTO deep_early (mint, wallet, ts, slot, sol, px, pre_move, bundled, symbol, added_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
             "ON CONFLICT (mint, wallet) DO NOTHING",
             (mint, b["wallet"], b["ts"], b["slot"], b["sol"], b["px"], b["pre_move"], b["bundled"], sym, now()))
    log.info("deep %s: %d early buyers (%d pre-move, %d bundled) via %s, %d credits", sym, len(early),
             sum(b["pre_move"] for b in early), sum(b["bundled"] for b in early), mode, tb.spent())
    return sym, len(early), why, tb.spent(), mode


# ---------------------------------------------------------------- wallets
def repeat_wallets(db, c):
    by = {}
    for r in db.all("SELECT wallet, mint, symbol FROM deep_early WHERE pre_move=1 AND bundled=0"):
        by.setdefault(r["wallet"], {})[r["mint"]] = r["symbol"]
    try:
        for r in db.all("SELECT wallet, mint, symbol FROM winner_early"):
            if r["wallet"] in by:
                by[r["wallet"]].setdefault(r["mint"], r["symbol"])
    except Exception:
        pass
    creators = {r["creator"] for r in db.all("SELECT DISTINCT creator FROM tokens WHERE creator IS NOT NULL")}
    bundled = {r["wallet"] for r in db.all("SELECT DISTINCT wallet FROM deep_early WHERE bundled=1")}
    out = [(w, m) for w, m in by.items() if len(m) >= c["min_repeat"] and w not in creators and w not in bundled]
    return sorted(out, key=lambda x: -len(x[1]))


async def last_activity(ctx, addr):
    sig = await ctx.helius.get_signatures(addr, limit=1, purpose=PURPOSE)
    return max([s.get("blockTime") or 0 for s in sig] or [0])


async def _promote(ctx, addr, mints, res, c):
    from .scorer import score_wallet
    db = ctx.db
    syms = ",".join(sorted({str(s or short(m)) for m, s in mints.items()}))[:150]
    db.x("INSERT INTO wallets (address, status, source, discovered_at) VALUES (?, 'candidate', ?, ?) ON CONFLICT (address) DO UPDATE "
         "SET status=CASE WHEN wallets.status IN ('watch','qualified') THEN wallets.status ELSE 'candidate' END, source=excluded.source",
         (addr, f"deep:{syms}", now()))
    w = db.one("SELECT * FROM wallets WHERE address=?", (addr,))
    _, status, _ = await score_wallet(ctx, w, PURPOSE)
    res["scored"][addr] = status


async def run_deep(ctx, manual=False):
    c, db = dcfg(ctx.cfg), ctx.db
    res = {"tokens": 0, "early": 0, "skipped": [], "credits": 0, "mode": None, "repeaters": 0, "insiders": 0,
           "dormant": 0, "revived": 0, "scored": {}, "stopped": None}
    if not c["enabled"] and not manual:
        res["stopped"] = "disabled"; return res
    ctx.cfg["budget"].setdefault("deep_share", 0.5)
    if ctx.budget.over_hard_stop():
        res["stopped"] = "monthly credit hard stop"; return res
    ensure_schema(db)
    start = ctx.budget.used(purpose=PURPOSE)
    spent = lambda: ctx.budget.used(purpose=PURPOSE) - start
    done = db.kv_get("deep_done") or {}
    for tk in await pick_tokens(ctx, done):
        left = c["max_credits_per_run"] - spent()
        if left < 20:
            res["stopped"] = "per-run credit cap"; break
        sym, n, why, cr, mode = await scan_token(ctx, tk, c, left)
        res["mode"] = res["mode"] or mode
        if n:
            res["tokens"] += 1; res["early"] += n
            done[tk["mint"]] = now()
        else:
            res["skipped"].append(f"{sym}: {why or 'no early buyers found'}")
            log.info("deep skip %s: %s", sym, why)
            if why and why.startswith("launched"):
                done[tk["mint"]] = now()  # out of range: don't look again
        if why and why.startswith("budget guard"):
            res["stopped"] = why; break
    db.kv_set("deep_done", done)

    try:
        reps = repeat_wallets(db, c)
        res["repeaters"] = len(reps)
        tagged = {r["address"]: r for r in db.all("SELECT * FROM wallet_tags")}
        n = 0
        for addr, mints in reps:
            if n >= c["wallets_per_run"] or res["stopped"]:
                break
            w = db.one("SELECT status FROM wallets WHERE address=?", (addr,)) or {}
            tg = tagged.get(addr)
            if tg and (tg.get("checked_at") or 0) > now() - c["dormant_recheck_days"] * DAY and w.get("status") in ("watch", "qualified", "dormant", "rejected"):
                continue
            n += 1
            last = await last_activity(ctx, addr)
            if last >= now() - c["active_days"] * DAY:
                set_tag(db, addr, "insider_likely", len(mints), sorted(mints), last)
                res["insiders"] += 1
                if w.get("status") not in ("watch", "qualified"):
                    await _promote(ctx, addr, mints, res, c)
            else:
                set_tag(db, addr, "dormant", len(mints), sorted(mints), last)
                db.x("INSERT INTO wallets (address, status, source, reason, discovered_at) VALUES (?, 'dormant', ?, ?, ?) "
                     "ON CONFLICT (address) DO UPDATE SET status=CASE WHEN wallets.status IN ('watch','qualified') THEN wallets.status "
                     "ELSE 'dormant' END, reason=excluded.reason",
                     (addr, "deep:" + ",".join(sorted(str(s) for s in mints.values()))[:150], "inactive >7d", now()))
                res["dormant"] += 1
        # weekly re-check of the dormant list (1 cheap probe each)
        for r in db.all("SELECT address, mints FROM wallet_tags WHERE tag='dormant' AND (checked_at IS NULL OR checked_at<?) "
                        "ORDER BY checked_at LIMIT ?", (now() - c["dormant_recheck_days"] * DAY, c["dormant_per_run"])):
            last = await last_activity(ctx, r["address"])
            if last >= now() - c["active_days"] * DAY:
                ms = {m: None for m in json.loads(r["mints"] or "[]")}
                set_tag(db, r["address"], "insider_likely", len(ms), sorted(ms), last)
                res["revived"] += 1
                await _promote(ctx, r["address"], ms, res, c)
            else:
                set_tag(db, r["address"], "dormant", last_active=last)
    except BudgetExceeded as e:
        res["stopped"] = f"budget guard: {e}"
    res["credits"] = spent()
    if any(s == "watch" for s in res["scored"].values()):
        await ctx.watcher.sync()
    db.kv_set("last_deep", {"ts": now(), **{k: v for k, v in res.items() if k not in ("scored", "skipped")},
                            "skipped": len(res["skipped"]), "scored": len(res["scored"])})
    db.event("deep", json.dumps({k: v for k, v in res.items() if k != "scored"})[:500])
    return res


def format_result(res):
    from .tgbot import esc, table
    if res.get("stopped") in ("disabled", "monthly credit hard stop") and not res["tokens"]:
        return f"🔭 <b>Deep winners scout</b>\nSkipped: {esc(res['stopped'])}"
    st = list(res["scored"].values())
    rows = [["Tokens scanned", str(res["tokens"])], ["Skipped", str(len(res["skipped"]))], ["Early buyers", str(res["early"])],
            ["Repeat early", str(res["repeaters"])], ["insider_likely", str(res["insiders"] + res["revived"])],
            ["Dormant", str(res["dormant"])], ["→ watch", str(st.count("watch"))], ["→ rejected", str(st.count("rejected"))],
            ["Credits", f"{res['credits']:,}"], ["History via", res.get("mode") or "-"]]
    out = "🔭 <b>Deep winners scout</b> · 1y · 📄 PAPER\n" + table(rows, "lr")
    if res["skipped"]:
        out += "<b>Skipped</b>\n" + esc("\n".join("• " + s for s in res["skipped"][:8]))
        if len(res["skipped"]) > 8:
            out += esc(f"\n… +{len(res['skipped']) - 8} more")
    if res["scored"]:
        out += "\n" + esc("\n".join(f"🎯 {short(a)} → {s}" for a, s in list(res["scored"].items())[:10]))
    if res.get("stopped"):
        out += f"\n<i>Stopped early: {esc(res['stopped'])}</i>"
    return out
