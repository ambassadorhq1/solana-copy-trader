"""Runtime settings via Telegram (/set, /settings). Overrides live in the DB (kv 'settings') so they survive redeploys and
take priority over config.yaml. Only the whitelisted knobs below can change; `mode` is not one of them, so no command can
enable live trading (paper is forced in build_context and re-checked here)."""
import copy, re

KV = "settings"

# key: (config path, kind, min, max, help)
KNOBS = {
    "trade_sol":       (("execution", "per_trade_sol"), "float", 0.01, None, "paper trade size, SOL"),
    "stop_loss":       (("execution", "stop_loss_mult"), "loss", 5, 95, "stop-loss, % loss (50 = sell at -50%)"),
    "time_stop_h":     (("execution", "time_stop_h"), "float", 1, 168, "time stop, hours"),
    "tp_ladder":       (("execution", "tp_ladder"), "ladder", None, None, "TP ladder, e.g. 50@2,50@3 (% of size @ multiple)"),
    "cluster_wallets": (("clusters", "min_wallets"), "int", 2, 25, "cluster: min watch wallets"),
    "cluster_window":  (("clusters", "window_min"), "int", 5, 1440, "cluster: window, minutes"),
    "cluster_sol":     (("clusters", "min_total_sol"), "float", 0.1, 10000, "cluster: min combined SOL"),
    "cluster_age_h":   (("clusters", "max_token_age_h"), "float", 1, 168, "cluster: max token age, hours"),
    "exit_mode":       (("execution", "default_exit"), "choice", None, None, "new trades: ladder (TP ladder) or follow (sell when the wallet sells)"),
}
CHOICES = {"exit_mode": {"ladder": "ladder", "default": "ladder", "tp": "ladder", "follow": "follow", "wallet": "follow"}}
ALIASES = {"per_trade_sol": "trade_sol", "size": "trade_sol", "sl": "stop_loss", "stop": "stop_loss", "time_stop": "time_stop_h",
           "ladder": "tp_ladder", "tp": "tp_ladder", "clusters.min_wallets": "cluster_wallets",
           "clusters.window_min": "cluster_window", "clusters.min_total_sol": "cluster_sol",
           "clusters.max_token_age_h": "cluster_age_h", "execution.per_trade_sol": "trade_sol",
           "execution.stop_loss_mult": "stop_loss", "execution.time_stop_h": "time_stop_h", "execution.tp_ladder": "tp_ladder"}
FORBIDDEN = {"mode", "live", "live_trading", "private_key", "wallet_key"}


def _get(cfg, path):
    cur = cfg
    for p in path:
        cur = (cur or {}).get(p)
    return cur


def _set(cfg, path, v):
    cur = cfg
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    if v is None:
        cur.pop(path[-1], None)
    else:
        cur[path[-1]] = v


def trade_cap(base):
    e = base["execution"]
    return min(float(e.get("max_per_trade_sol", e["per_trade_sol"])), float(e["daily_cap_sol"]))


def parse_ladder(s):
    s = s.strip().lower()
    if s in ("off", "none", "follow"):
        raise ValueError("a ladder needs at least one tranche (use /exit <id> follow for one trade)")
    out = []
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)%?@(\d+(?:\.\d+)?)x?", part)
        if not m:
            raise ValueError(f"bad tranche '{part}': use pct@mult, e.g. 50@2")
        frac, mult = float(m.group(1)), float(m.group(2))
        frac = frac / 100 if frac > 1 else frac
        if not (0 < frac <= 1) or not (1.1 <= mult <= 1000):
            raise ValueError(f"tranche '{part}': size 1–100%, multiple 1.1–1000x")
        out.append({"mult": mult, "frac": round(frac, 6)})
    if not 1 <= len(out) <= 4:
        raise ValueError("use 1 to 4 tranches")
    if any(b["mult"] <= a["mult"] for a, b in zip(out, out[1:])):
        raise ValueError("multiples must go up, e.g. 50@2,50@3")
    if sum(t["frac"] for t in out) > 1 + 1e-9:
        raise ValueError("tranche sizes add up to more than 100%")
    return out


def validate(key, raw, base):
    path, kind, lo, hi, _ = KNOBS[key]
    if kind == "ladder":
        return parse_ladder(raw)
    if kind == "choice":
        v = CHOICES[key].get(raw.strip().lower())
        if not v:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(set(CHOICES[key].values())))}")
        return v
    raw = raw.strip().rstrip("%").lstrip("-") if kind == "loss" else raw.strip().rstrip("hmx").replace("sol", "").strip()
    try:
        v = int(raw) if kind == "int" else float(raw)
    except ValueError:
        raise ValueError(f"{key} needs a number")
    if key == "trade_sol":
        hi = trade_cap(base)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}" + (" (per-trade and daily caps; raise execution.max_per_trade_sol "
                                                                      "in config.yaml to allow more)" if key == "trade_sol" else ""))
    return round(1 - v / 100, 4) if kind == "loss" else v


def apply(ctx):
    """Re-apply DB overrides on top of the config.yaml values (ctx.cfg_base). Mutates ctx.cfg in place so every
    component holding a reference (executor.c, clusters, exits) sees the change."""
    if not hasattr(ctx, "cfg_base"):
        ctx.cfg_base = copy.deepcopy(ctx.cfg)
    ov = ctx.db.kv_get(KV) or {}
    for key, (path, *_rest) in KNOBS.items():
        base_v = _get(ctx.cfg_base, path)
        _set(ctx.cfg, path, copy.deepcopy(base_v))
        if key in ov:
            try:
                v = ov[key]
                if key == "trade_sol":
                    v = min(float(v), trade_cap(ctx.cfg_base))  # caps may have been lowered in config.yaml since
                _set(ctx.cfg, path, v)
            except Exception:
                pass
    ctx.cfg["mode"] = "paper"  # forced, whatever is stored anywhere
    return ov


def set_value(ctx, key, raw):
    """-> (ok, message). raw 'default' / 'reset' removes the override."""
    k = key.strip().lower()
    if k in FORBIDDEN or "live" in k or "mode" == k.split(".")[-1]:
        return False, "Paper mode is forced. Live trading can't be enabled from Telegram."
    k = ALIASES.get(k, k)
    if k not in KNOBS:
        return False, "Unknown setting. See /settings for the keys."
    if not hasattr(ctx, "cfg_base"):
        apply(ctx)
    ov = ctx.db.kv_get(KV) or {}
    if raw.strip().lower() in ("default", "reset", "config"):
        ov.pop(k, None)
        ctx.db.kv_set(KV, ov); apply(ctx)
        return True, f"{k} reset to config.yaml: {show(ctx, k)}"
    try:
        v = validate(k, raw, ctx.cfg_base)
    except ValueError as e:
        return False, str(e)
    ov[k] = v
    ctx.db.kv_set(KV, ov); apply(ctx)
    note = " (new trades only; open positions keep their ladder)" if k in ("tp_ladder", "exit_mode") else ""
    return True, f"{k} = {show(ctx, k)}{note}"


def show(ctx, key):
    path, kind, *_ = KNOBS[key]
    v = _get(ctx.cfg, path)
    if kind == "ladder":
        from .exits import config_ladder
        from .tgbot import ladder
        return ladder(config_ladder(ctx.cfg["execution"]))
    if kind == "choice":
        return v or "ladder"
    if kind == "loss":
        return f"-{(1 - float(v)) * 100:.1f}%"
    if key == "trade_sol":
        return f"{float(v):.3f} SOL"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def overrides(ctx):
    return ctx.db.kv_get(KV) or {}
