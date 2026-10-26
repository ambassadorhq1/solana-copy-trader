"""Exit analytics: how far do profitable wallets' trades actually run, and which TP ladder fits OUR delayed entry?

Data sources (no extra API calls, no extra Helius credits):
  * `trades`        every swap the app has stored (watched + scored wallets). A swap's SOL/token ratio is a real
                    on-chain price point for that mint at that second, so the trades of *all* stored wallets on a
                    mint form a sparse price history. Peaks from it are LOWER BOUNDS (wicks between swaps are unseen).
  * `price_samples` the executor's own 30 s Dexscreener samples of every open paper position (dense, ~24 h).
  Dexscreener has no free price-history endpoint, so it only contributes through those live samples.

Pieces:
  closed_trades()   per (wallet, mint) closed trade: size-weighted realized exit multiples, partial sells included.
  build_cases()     adds OUR simulated entry (wallet first-buy price x (1 + entry_delay_slippage)) or, for closed
                    paper positions, our real entry, plus the price path for the next `peak_window_h` hours.
  distribution()    weighted p25/median/p75 of exit multiples + hit rates (wallet realized, and our 24h peak).
  simulate()        replays one case through a ladder with the bot's own rules (TP tranches, follow-the-wallet
                    sells, stop-loss, time stop, simulated slippage + fees) -> our realized multiple.
  recommend()       grid-searches ladders; keeps those whose first tranche is reached by >= min_first_hit of cases
                    and whose every level was reached by >= min_level_hits cases; picks the best mean multiple.
"""
import json

from .util import log, now, short

DAY = 86400


# ---------------------------------------------------------------- helpers
def _px(r):
    """SOL per UI token of a stored swap row."""
    if not r.get("tokens") or not r.get("sol"):
        return None
    return r["sol"] / (r["tokens"] / 10 ** (r.get("decimals") or 0))


def wquantile(pairs, q):
    """Weighted quantile of [(value, weight)]."""
    pairs = sorted((v, w) for v, w in pairs if w and w > 0)
    if not pairs:
        return None
    tot, acc = sum(w for _, w in pairs), 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot - 1e-12:
            return v
    return pairs[-1][0]


def ecfg(cfg):
    d = {"entry_delay_slippage": 0.15, "peak_window_h": 24, "hit_levels": [2, 3, 5, 10, 20, 50, 100],
         "ladder_levels": [1.5, 2, 3, 5, 10, 20, 50, 100], "ladder_fracs": [0.25, 0.5, 0.75, 1.0],
         "max_tranches": 3, "min_first_hit": 0.6, "min_level_hits": 3, "min_cases": 15,
         "auto_tune_exits": False, "auto_tune_min_paper_trades": 30, "sample_retention_days": 30,
         "reco_max_age_h": 26}
    d.update(cfg.get("exits") or {})
    return d


def config_ladder(c):
    """execution.tp_ladder if set, else the classic tp1/tp2 pair. Each tranche: mult + frac of the INITIAL size."""
    if c.get("tp_ladder"):
        return [{"mult": float(t["mult"]), "frac": float(t["frac"])} for t in c["tp_ladder"]]
    return [{"mult": float(c["tp1_mult"]), "frac": float(c["tp1_frac"])},
            {"mult": float(c["tp2_mult"]), "frac": round(1 - float(c["tp1_frac"]), 6)}]


def fmt_ladder(lad):
    if not lad:
        return "no TP (follow / SL / time stop only)"
    parts = [f"{t['frac']:.0%}@{t['mult']:g}x" for t in lad]
    rest = 1 - sum(t["frac"] for t in lad)
    if rest > 1e-3:
        parts.append(f"{rest:.0%} runner (follow/SL/time stop)")
    return ", ".join(parts)


def config_snippet(lad):
    rows = "\n".join(f"    - {{mult: {t['mult']:g}, frac: {t['frac']:g}}}" for t in lad)
    return "execution:\n  tp_ladder:\n" + rows


# ---------------------------------------------------------------- (1) realized exits per closed trade
def closed_trades(rows, cfg, t=None):
    """rows: one wallet's stored swaps. -> closed trades with size-weighted exit multiples (partial sells count)."""
    sc, t = cfg["scoring"], t or now()
    since = t - sc["window_days"] * DAY
    by = {}
    for r in rows:
        if r["side"] in ("buy", "sell") and r["ts"] >= since:
            by.setdefault(r["mint"], []).append(r)
    out = []
    for mint, rs in by.items():
        rs.sort(key=lambda r: (r["ts"], r["side"] != "buy"))
        buys = [r for r in rs if r["side"] == "buy"]
        if not buys or _px(buys[0]) is None:
            continue
        bt = sum(r["tokens"] for r in buys)
        st = sum(r["tokens"] for r in rs if r["side"] == "sell" and r["ts"] >= buys[0]["ts"])
        if st < sc["closed_sold_frac"] * bt:
            continue  # still open (same rule as the scorer)
        b_tok = b_sol = 0.0
        held_sold = 0.0
        exits, sells = [], []
        for r in rs:
            if r["side"] == "buy":
                b_tok += r["tokens"]; b_sol += r["sol"]; continue
            if not b_tok:
                continue  # sell before any buy (tokens came from elsewhere)
            avg = b_sol / (b_tok / 10 ** (r.get("decimals") or 0))
            px = _px(r)
            q = min(r["tokens"], max(b_tok - held_sold, 0))  # never count more than was bought
            held_sold += q
            if px and q > 0:
                exits.append((px / avg, q / 10 ** (r.get("decimals") or 0) * avg))  # weight = SOL cost basis sold
            pct = min(1.0, r["tokens"] / r["pre_tokens"]) if r.get("pre_tokens") else 1.0
            sells.append({"ts": r["ts"], "pct": pct, "px": px})
        if not exits:
            continue
        sol_in = sum(r["sol"] for r in buys)
        sol_out = sum(r["sol"] for r in rs if r["side"] == "sell") * min(1.0, bt / st)
        out.append({"wallet": buys[0]["wallet"], "mint": mint, "entry_ts": buys[0]["ts"], "entry_px": _px(buys[0]),
                    "exits": exits, "best_exit": max(m for m, _ in exits),
                    "realized_mult": sol_out / sol_in if sol_in else 0, "sells": sells})
    return out


# ---------------------------------------------------------------- (2) our delayed entry + 24h price path
def _chunks(xs, n=400):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def load_price_points(db, mints):
    """{mint: sorted [(ts, px, src)]} from every stored swap on the mint + the bot's own 30 s samples."""
    pts = {}
    for ch in _chunks(set(mints)):
        ph = ",".join("?" * len(ch))
        for r in db.all(f"SELECT mint, ts, sol, tokens, decimals FROM trades WHERE side IN ('buy','sell') AND mint IN ({ph})", ch):
            p = _px(r)
            if p:
                pts.setdefault(r["mint"], []).append((r["ts"], p, "swap"))
        for r in db.all(f"SELECT mint, ts, price FROM price_samples WHERE mint IN ({ph})", ch):
            if r["price"]:
                pts.setdefault(r["mint"], []).append((r["ts"], r["price"], "sample"))
    for v in pts.values():
        v.sort()
    return pts


def _case(base, entry_ts, our_px, pts, sells, win_s, kind):
    inwin = [(ts, p, s) for ts, p, s in pts if entry_ts < ts <= entry_ts + win_s]
    after = next(((ts, p, s) for ts, p, s in pts if ts > entry_ts + win_s), None)
    peak = max(p for _, p, _ in inwin) / our_px if inwin else None
    srcs = {s for _, _, s in inwin}
    return dict(base, kind=kind, entry_ts=entry_ts, our_px=our_px, path=[(ts, p / our_px) for ts, p, _ in inwin],
                after=(after[1] / our_px) if after else None,
                sells=[dict(s, mult=(s["px"] / our_px) if s["px"] else None) for s in sells
                       if entry_ts < s["ts"]],
                peak=peak, peak_src=("samples" if "sample" in srcs else "swaps") if inwin else None)


def build_cases(db, cfg, wallets, t=None):
    """Closed wallet trades (simulated delayed entry) + closed paper positions (real entry) for `wallets`."""
    e, t = ecfg(cfg), t or now()
    win_s = e["peak_window_h"] * 3600
    wallets = list(wallets)
    trades = []
    for w in wallets:
        rows = db.all("SELECT * FROM trades WHERE wallet=? AND ts>=?", (w, t - cfg["scoring"]["window_days"] * DAY))
        trades += closed_trades(rows, cfg, t)
    papers = []
    for ch in _chunks(wallets):
        papers += db.all("SELECT * FROM positions WHERE status='closed' AND wallet IN (%s)" % ",".join("?" * len(ch)), ch)
    pts = load_price_points(db, [x["mint"] for x in trades] + [p["mint"] for p in papers])
    paper_keys = {(p["wallet"], p["mint"]) for p in papers}
    cases = []
    for tr in trades:
        if (tr["wallet"], tr["mint"]) in paper_keys:
            continue  # our real paper trade on the same token replaces the simulated one
        our = tr["entry_px"] * (1 + e["entry_delay_slippage"])
        cases.append(_case(tr, tr["entry_ts"], our, pts.get(tr["mint"], []), tr["sells"], win_s, "wallet"))
    if papers:
        wsells = {}
        for ch in _chunks({p["mint"] for p in papers}):
            for r in db.all("SELECT * FROM trades WHERE side='sell' AND mint IN (%s)" % ",".join("?" * len(ch)), ch):
                wsells.setdefault((r["wallet"], r["mint"]), []).append(
                    {"ts": r["ts"], "pct": min(1.0, r["tokens"] / r["pre_tokens"]) if r["pre_tokens"] else 1.0, "px": _px(r)})
        for p in papers:
            # samples are exact; our max_mult (also from those samples) backs it up if old samples were pruned
            c = _case({"wallet": p["wallet"], "mint": p["mint"], "exits": [], "best_exit": None,
                       "realized_mult": p["sol_out"] / p["sol_in"] if p["sol_in"] else 0},
                      p["opened_ts"], p["entry_price"], pts.get(p["mint"], []),
                      sorted(wsells.get((p["wallet"], p["mint"]), []), key=lambda s: s["ts"]), win_s, "paper")
            if p.get("max_mult") and (c["peak"] or 0) < p["max_mult"]:
                c["peak"], c["peak_src"] = p["max_mult"], "samples"
            cases.append(c)
    return cases


# ---------------------------------------------------------------- distribution + hit rates
def distribution(cases, cfg):
    e = ecfg(cfg)
    levels = e["hit_levels"]
    wc = [c for c in cases if c["kind"] == "wallet"]
    ex = [x for c in wc for x in c["exits"]]
    pk = [c for c in cases if c["peak"] is not None]
    return {
        "n": len(cases), "n_wallet_trades": len(wc), "n_paper": len(cases) - len(wc), "n_peak": len(pk),
        "wallets": len({c["wallet"] for c in cases}),
        "p25": wquantile(ex, 0.25), "median": wquantile(ex, 0.5), "p75": wquantile(ex, 0.75),
        "wallet_hit": {L: sum(c["best_exit"] >= L for c in wc) / len(wc) for L in levels} if wc else {},
        "our_hit": {L: sum(c["peak"] >= L for c in pk) / len(pk) for L in levels} if pk else {},
        "peak_from_samples": sum(c["peak_src"] == "samples" for c in pk),
        "delay": e["entry_delay_slippage"],
    }


# ---------------------------------------------------------------- (3) ladder simulation + recommender
def simulate(case, ladder, cfg, follow=True):
    """Our realized multiple for one case under `ladder`, using the executor's rules. None if no price data."""
    c = cfg["execution"]
    if not case["path"] and not case["sells"]:
        return None
    cost = c["sim_slippage_bps"] / 1e4
    fee = c["sim_fee_sol"] / c["per_trade_sol"]
    ev = [(ts, 0, m, None) for ts, m in case["path"]]
    ev += [(s["ts"], 1, s["mult"], s["pct"]) for s in case["sells"] if follow and s["mult"] is not None]
    ev.sort(key=lambda x: (x[0], x[1]))
    t_end = case["entry_ts"] + c["time_stop_h"] * 3600
    left, got, done, last = 1.0, 0.0, 0, None

    def sell(q, m):
        nonlocal left, got
        q = min(q, left)
        if q > 1e-9:
            got += q * m * (1 - cost) - fee
            left -= q

    timed_out, after_px = False, case["after"]
    for ts, kind, m, pct in ev:
        if left <= 1e-9:
            break
        if ts > t_end:
            timed_out, after_px = True, (after_px if after_px is not None else m)
            break
        last = m
        if kind == 0:
            if m <= c["stop_loss_mult"]:
                sell(left, m); break
            while done < len(ladder) and m >= ladder[done]["mult"]:
                cum = sum(t["frac"] for t in ladder[:done + 1])
                sell(left if cum >= 0.999 else ladder[done]["frac"], ladder[done]["mult"])
                done += 1
        else:
            sell(left if pct >= 0.98 else left * pct, m)
    if left > 1e-9:
        # time stop at 24 h: the price we know closest to that moment (last seen before it, else first after it)
        m = last if last is not None else after_px
        if m is None:
            return None
        sell(left, m)
    return max(got, 0.0) / (1 + fee)


def _ladders(e):
    from itertools import combinations
    fr = e["ladder_fracs"]
    out = [[]]
    for k in range(1, e["max_tranches"] + 1):
        for lv in combinations(sorted(e["ladder_levels"]), k):
            def rec(i, acc, s):
                if i == k:
                    out.append([{"mult": float(lv[j]), "frac": acc[j]} for j in range(k)]); return
                for f in fr:
                    if s + f <= 1 + 1e-9:
                        rec(i + 1, acc + [f], s + f)
            rec(0, [], 0.0)
    return out


def _evaluate(sim, ladder, cfg):
    r = [x for x in (simulate(c, ladder, cfg) for c in sim) if x is not None]
    if not r:
        return None
    r.sort()
    return {"mean": sum(r) / len(r), "median": r[len(r) // 2], "p_loss": sum(x < 1 for x in r) / len(r), "n": len(r)}


def recommend(cases, cfg):
    e = ecfg(cfg)
    sim = [c for c in cases if c["peak"] is not None]
    base = config_ladder(cfg["execution"])
    if len(sim) < e["min_cases"]:
        return {"ok": False, "reason": f"only {len(sim)} trades with price data (need {e['min_cases']})", "n": len(sim)}
    hits = {L: sum(c["peak"] >= L for c in sim) for L in e["ladder_levels"]}
    best = None
    for lad in _ladders(e):
        if lad and hits.get(lad[0]["mult"], 0) / len(sim) < e["min_first_hit"]:
            continue  # safety bias: the first tranche must actually be reached by most trades
        if any(hits.get(t["mult"], 0) < e["min_level_hits"] for t in lad):
            continue  # don't chase a level only one or two lucky trades reached
        if not lad:
            continue
        r = _evaluate(sim, lad, cfg)
        if r and (best is None or r["mean"] > best[1]["mean"] + 1e-9):
            best = (lad, r)
    if not best:
        return {"ok": False, "n": len(sim),
                "reason": f"no ladder level is reached by >= {e['min_first_hit']:.0%} of trades after our entry"}
    lad, r = best
    return {"ok": True, "ladder": lad, "expected": r["mean"], "median": r["median"], "p_loss": r["p_loss"],
            "n": r["n"], "first_hit": hits[lad[0]["mult"]] / len(sim),
            "current": _evaluate(sim, base, cfg), "follow_only": _evaluate(sim, [], cfg),
            "config": config_snippet(lad)}


# ---------------------------------------------------------------- entry points used by the app
def watch_wallets(db):
    return [r["address"] for r in db.all("SELECT address FROM wallets WHERE status='watch'")]


def analyze(ctx, wallet=None):
    wallets = [wallet] if wallet else watch_wallets(ctx.db)
    cases = build_cases(ctx.db, ctx.cfg, wallets)
    return distribution(cases, ctx.cfg), recommend(cases, ctx.cfg)


def refresh_reco(ctx):
    dist, reco = analyze(ctx)
    ctx.db.kv_set("exit_reco", {"ts": now(), "dist": {k: v for k, v in dist.items() if not isinstance(v, dict)}, "reco": reco})
    return dist, reco


async def entry_ladder(ctx):
    """Ladder for a NEW paper trade: config, or the recommendation when auto_tune_exits is on and earned.
    Open positions keep the ladder they were opened with (stored on the position)."""
    import asyncio
    e, c = ecfg(ctx.cfg), ctx.cfg["execution"]
    lad = config_ladder(c)
    if not e["auto_tune_exits"]:
        return lad, "config"
    n_closed = ctx.db.val("SELECT COUNT(*) AS n FROM positions WHERE status='closed'", (), 0)
    if n_closed < e["auto_tune_min_paper_trades"]:
        return lad, f"config (auto-tune after {e['auto_tune_min_paper_trades']} closed paper trades, have {n_closed})"
    cached = ctx.db.kv_get("exit_reco") or {}
    reco = cached.get("reco") or {}
    if now() - (cached.get("ts") or 0) > e["reco_max_age_h"] * 3600:
        try:
            _, reco = await asyncio.to_thread(refresh_reco, ctx)
        except Exception as ex:
            log.exception("exit recommendation failed: %s", ex)
            return lad, "config (recommendation failed)"
    if reco.get("ok"):
        return reco["ladder"], "auto-tuned"
    return lad, f"config ({reco.get('reason', 'no recommendation')})"


def _x(v):
    return f"{v:.2f}x" if v is not None else "-"


def format_exits(ctx, dist, reco, wallet=None):
    who = short(wallet) if wallet else f"watchlist ({dist['wallets']} wallet{'s' if dist['wallets'] != 1 else ''})"
    if not dist["n"]:
        return f"📐 Exits — {who}\nNo closed trades yet."
    levels = ecfg(ctx.cfg)["hit_levels"]
    head = "     " + " ".join(f"{L:>4g}x" for L in levels)
    row = lambda name, h: name + " ".join(f"{h.get(L, 0):>5.0%}" for L in levels) if h else name + " no data"
    lines = [f"📐 Exits — {who}",
             f"{dist['n_wallet_trades']} closed wallet trades + {dist['n_paper']} closed paper trades",
             f"Wallet exits (size-weighted): p25 {_x(dist['p25'])} · median {_x(dist['median'])} · p75 {_x(dist['p75'])}",
             "Hit rate (share of trades reaching ≥ level)", head,
             row("Wal  ", dist["wallet_hit"]),
             row("Us   ", dist["our_hit"]),
             f"Wal = wallet's best realized sell. Us = 24h peak after our entry (wallet price +{dist['delay']:.0%}; "
             f"{dist['n_peak']} trades with price data, {dist['peak_from_samples']} from our 30s samples, rest from swap prices = lower bound)."]
    if reco.get("ok"):
        cur, fo = reco.get("current") or {}, reco.get("follow_only") or {}
        lines += [f"\n✅ Suggested ladder: {fmt_ladder(reco['ladder'])}",
                  f"Sim: {reco['expected']:.2f}x mean/trade (median {reco['median']:.2f}x, {reco['p_loss']:.0%} losers) · "
                  f"first tranche reached by {reco['first_hit']:.0%}",
                  f"vs current {fmt_ladder(config_ladder(ctx.cfg['execution']))}: {_x(cur.get('mean'))} · follow-only {_x(fo.get('mean'))}",
                  "Suggested config.yaml:\n" + reco["config"]]
        if fo.get("mean") and fo["mean"] > reco["expected"]:
            lines.insert(-1, "Note: following the wallet with no TP simulated higher; the ladder is kept because it banks profit on "
                             "most trades and our real exits lag the wallet's (sim assumes we sell at its price).")
    else:
        lines.append(f"\nNo ladder suggested: {reco.get('reason')}")
    e = ecfg(ctx.cfg)
    lines.append(f"auto_tune_exits: {'on' if e['auto_tune_exits'] else 'off'}")
    return "\n".join(lines)


def summary_line(ctx):
    dist, reco = refresh_reco(ctx)
    if not dist["n"]:
        return "Exits: no closed trades yet."
    us2 = dist["our_hit"].get(2)
    s = (f"Exits: wallet median {_x(dist['median'])} (p25 {_x(dist['p25'])}–p75 {_x(dist['p75'])}), "
         f"us ≥2x {us2:.0%}" if us2 is not None else f"Exits: wallet median {_x(dist['median'])}")
    return s + (f" · suggested {fmt_ladder(reco['ladder'])} ({reco['expected']:.2f}x/trade)" if reco.get("ok")
                else f" · no ladder yet ({reco.get('reason')})")


def resolve_wallet(db, arg):
    """Full address, prefix, or the short 'AbCd…WxYz' form used in alerts."""
    arg = arg.strip()
    if "…" in arg or "..." in arg:
        a, b = arg.replace("...", "…").split("…", 1)
        rows = db.all("SELECT address FROM wallets WHERE address LIKE ?", (f"{a}%{b}",))
    else:
        rows = db.all("SELECT address FROM wallets WHERE address LIKE ?", (f"{arg}%",))
    return [r["address"] for r in rows]


def prune_samples(ctx):
    days = ecfg(ctx.cfg)["sample_retention_days"]
    ctx.db.x("DELETE FROM price_samples WHERE ts<?", (now() - days * DAY,))
