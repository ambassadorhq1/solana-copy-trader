"""Swap detection from raw getTransaction-shaped JSON (gTFA full, getTransaction, raw webhooks).
Uses pre/post token balances + lamport deltas; SOL/WSOL-quoted swaps only (v1)."""
from .util import WSOL, STABLES


def keys_of(tx):
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(la.get("writable") or []) + list(la.get("readonly") or [])


def sig_of(tx):
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else tx.get("signature")


def fee_payer(tx):
    k = keys_of(tx)
    return k[0] if k else None


def parse_swaps(tx, owner=None, min_sol=0.001):
    """-> list of {wallet, mint, side(buy|sell|in|out), sol, tokens, decimals, pre_tokens, ts, slot, sig}"""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return []
    keys = keys_of(tx)
    if not keys:
        return []
    owner = owner or keys[0]
    deltas = {}  # mint -> [raw delta, decimals, pre raw]
    for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(field) or []:
            if b.get("owner") != owner:
                continue
            ui = b.get("uiTokenAmount") or {}
            amt = int(ui.get("amount") or 0)
            d = deltas.setdefault(b["mint"], [0, int(ui.get("decimals") or 0), 0])
            d[0] += sign * amt
            if sign < 0:
                d[2] += amt
    lamports = 0
    if owner in keys:
        i = keys.index(owner)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            lamports = post[i] - pre[i]
        if i == 0:
            lamports += meta.get("fee") or 0  # tx fee is not trade cost
    if WSOL in deltas:
        lamports += deltas.pop(WSOL)[0]
    sol = lamports / 1e9
    if any(deltas.get(m, [0])[0] for m in STABLES):
        return []  # stable-quoted swap: skipped in v1
    moved = {m: d for m, d in deltas.items() if d[0]}
    if len(moved) != 1:
        return []  # nothing, or token<->token route: ambiguous
    mint, (delta, dec, pre_raw) = next(iter(moved.items()))
    if delta > 0 and sol <= -min_sol:
        side, amt = "buy", -sol
    elif delta < 0 and sol >= min_sol:
        side, amt = "sell", sol
    elif delta > 0 and abs(sol) < min_sol:
        side, amt = "in", 0.0  # received without paying: transfer / airdrop / insider supply
    elif delta < 0 and abs(sol) < min_sol:
        side, amt = "out", 0.0
    else:
        return []
    return [{"wallet": owner, "mint": mint, "side": side, "sol": amt, "tokens": float(abs(delta)),
             "decimals": dec, "pre_tokens": float(pre_raw), "ts": tx.get("blockTime") or 0,
             "slot": tx.get("slot") or 0, "sig": sig_of(tx)}]


def sol_funder(tx, wallet):
    """Account whose lamports dropped most while `wallet` received SOL (a funding transfer)."""
    meta, keys = tx.get("meta") or {}, keys_of(tx)
    if wallet not in keys:
        return None
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    deltas = [(post[i] - pre[i], k) for i, k in enumerate(keys) if i < len(pre) and i < len(post)]
    mine = dict((k, d) for d, k in deltas).get(wallet, 0)
    if mine <= 0:
        return None
    d, k = min(deltas)
    return k if d < 0 and k != wallet else None
