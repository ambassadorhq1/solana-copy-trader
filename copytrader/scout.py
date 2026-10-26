"""Scout: recently pumped Solana tokens (Dexscreener) -> early buyers from the mint's oldest txs (Helius gTFA asc)."""
from .clients import BudgetExceeded, HeliusError
from .parse import parse_swaps, fee_payer
from .util import is_excluded, log, now


def early_buyers(txs, mint, cfg):
    """Fee-payer wallets that bought `mint` early, skipping the creator and launch-slot snipers."""
    if not txs:
        return None, []
    txs = sorted(txs, key=lambda t: (t.get("slot") or 0, t.get("blockTime") or 0))
    first = txs[0]
    creator, first_slot, first_ts = fee_payer(first), first.get("slot") or 0, first.get("blockTime") or 0
    seen, out = set(), []
    for tx in txs:
        if (tx.get("slot") or 0) < first_slot + cfg["sniper_slots"]:
            continue
        if (tx.get("blockTime") or 0) > first_ts + cfg["early_window_min"] * 60:
            break
        for s in parse_swaps(tx):
            if s["mint"] == mint and s["side"] == "buy" and s["wallet"] != creator and s["wallet"] not in seen:
                seen.add(s["wallet"]); out.append(s)
    return {"creator": creator, "first_slot": first_slot, "first_ts": first_ts}, out[: cfg["max_buyers_per_token"]]


async def pick_tokens(ctx):
    c = ctx.cfg["scout"]
    mints = await ctx.dex.discover_mints()
    pairs = await ctx.dex.best_pairs(mints)
    picks, t = [], now()
    for mint, p in pairs.items():
        if is_excluded(mint, (p.get("baseToken") or {}).get("symbol")):
            continue
        created = (p.get("pairCreatedAt") or 0) / 1000
        age_h = (t - created) / 3600 if created else 1e9
        pc = p.get("priceChange") or {}
        change = max(pc.get("h24") or 0, pc.get("h6") or 0)
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if c["min_pair_age_h"] <= age_h <= c["max_pair_age_h"] and change >= c["min_change_pct"] and liq >= c["min_liquidity_usd"]:
            if not ctx.db.one("SELECT 1 AS x FROM tokens WHERE mint=? AND scouted_at IS NOT NULL", (mint,)):
                picks.append((change, mint, p))
    picks.sort(key=lambda x: -x[0])
    return picks[: c["tokens_per_run"]]


async def run_scout(ctx):
    c = ctx.cfg["scout"]
    if ctx.budget.over_hard_stop():
        log.warning("scout skipped: monthly credit hard stop"); return {"tokens": 0, "candidates": 0}
    n_tok = n_cand = 0
    for change, mint, p in await pick_tokens(ctx):
        try:
            txs = await ctx.helius.history(mint, "scout", asc=True, limit=min(c["early_tx_limit"], 1000))
        except BudgetExceeded as e:
            log.info("scout budget reached: %s", e); break
        except HeliusError as e:
            log.warning("scout: history for %s failed: %s", mint, e)
            if e.code in (401, 403):
                break
            continue
        info, buyers = early_buyers(txs, mint, c)
        sym = (p.get("baseToken") or {}).get("symbol")
        ctx.db.x("INSERT INTO tokens (mint, symbol, pair_created_at, creator, first_slot, scouted_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT (mint) DO UPDATE SET creator=excluded.creator, first_slot=excluded.first_slot, scouted_at=excluded.scouted_at, "
                 "symbol=excluded.symbol, pair_created_at=excluded.pair_created_at",
                 (mint, sym, int((p.get("pairCreatedAt") or 0) / 1000) or (info or {}).get("first_ts"),
                  (info or {}).get("creator"), (info or {}).get("first_slot"), now()))
        n_tok += 1
        for b in buyers:
            cur = ctx.db.x("INSERT INTO wallets (address, status, source, discovered_at) VALUES (?, 'candidate', ?, ?) "
                           "ON CONFLICT (address) DO NOTHING", (b["wallet"], f"early:{sym}", now()))
            n_cand += max(cur.rowcount, 0)
        log.info("scout %s (+%.0f%%): %d early buyers", sym, change, len(buyers))
    from .scorer import process_candidates
    scored = await process_candidates(ctx, c["backfill_per_run"])
    ctx.db.kv_set("last_scout", {"ts": now(), "tokens": n_tok, "new_candidates": n_cand, "scored": scored})
    ctx.db.event("scout", f"tokens={n_tok} new_candidates={n_cand} scored={scored}")
    return {"tokens": n_tok, "candidates": n_cand, "scored": scored}
