"""Web dashboard: GET /dashboard (one self-contained HTML page) + GET /api/summary (JSON, cached ~10 s), plus two
key-protected writes: POST /api/settings (same whitelist + validation as Telegram /set, see settings.py) and
POST /api/action (pause / resume / scout / deep). Live mode can't be enabled from here: paper stays forced.

Auth: the key is env DASHBOARD_KEY, else TG_WEBHOOK_SECRET (ctx.tg_secret). Pass ?key=... once; /dashboard then sets an
HttpOnly SameSite=Lax cookie. Constant-time compare, 401 on a missing or bad key. POSTs must be application/json.
Exit stats come from the cached daily recommendation (kv 'exit_reco'); hit rates are computed in a background thread at
most once per new recommendation (or per reco_max_age_h), never inside a request.
"""
import asyncio, hmac, json, os, time
from decimal import Decimal

from aiohttp import web

from .util import day_start_ts, log, now, short

COOKIE = "ct_dash"
CACHE_S = 10


def dashboard_key(ctx):
    return getattr(ctx, "dashboard_key", None) or os.environ.get("DASHBOARD_KEY") or ctx.tg_secret or ""


def _authorized(ctx, req):
    key = dashboard_key(ctx)
    given = req.query.get("key") or req.cookies.get(COOKIE) or ""
    return bool(key) and bool(given) and hmac.compare_digest(given.encode(), key.encode())


def _json(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o).__name__)


HDRS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


def _deny():
    return web.Response(status=401, text="unauthorized", headers=HDRS)


# ---------------------------------------------------------------- exit hit rates (background, never per request)
def _exit_hits(ctx):
    from .exits import build_cases, distribution, ecfg, watch_wallets
    cases = build_cases(ctx.db, ctx.cfg, watch_wallets(ctx.db))
    d = distribution(cases, ctx.cfg)
    return {"wallet_hit": {f"{k:g}": v for k, v in d["wallet_hit"].items()}, "our_hit": {f"{k:g}": v for k, v in d["our_hit"].items()},
            "levels": ecfg(ctx.cfg)["hit_levels"]}


def _maybe_refresh_hits(ctx, reco_ts):
    from .exits import ecfg
    st = ctx.__dict__.setdefault("_dash", {})
    task = st.get("hits_task")
    if task and not task.done():
        return
    cached = ctx.db.kv_get("dash_exit_hits") or {}
    max_age = ecfg(ctx.cfg)["reco_max_age_h"] * 3600
    stale = not cached or (reco_ts or 0) > (cached.get("reco_ts") or 0) or now() - (cached.get("ts") or 0) > max_age
    if not stale or now() - st.get("hits_tried", 0) < 600:
        return
    st["hits_tried"] = now()

    async def run():
        try:
            hits = await asyncio.to_thread(_exit_hits, ctx)
            ctx.db.kv_set("dash_exit_hits", dict(hits, ts=now(), reco_ts=reco_ts or 0))
        except Exception as e:
            log.exception("dashboard exit hits failed: %s", e)
    st["hits_task"] = asyncio.get_running_loop().create_task(run())


def _exits(ctx):
    from .exits import config_ladder
    from .tgbot import ladder
    cached = ctx.db.kv_get("exit_reco") or {}
    _maybe_refresh_hits(ctx, cached.get("ts"))
    hits = ctx.db.kv_get("dash_exit_hits") or {}
    reco, dist = cached.get("reco") or {}, cached.get("dist") or {}
    out = {"as_of": cached.get("ts"), "n": dist.get("n"), "n_wallet_trades": dist.get("n_wallet_trades"),
           "n_paper": dist.get("n_paper"), "median": dist.get("median"), "p25": dist.get("p25"), "p75": dist.get("p75"),
           "levels": hits.get("levels") or [], "wallet_hit": hits.get("wallet_hit") or {}, "our_hit": hits.get("our_hit") or {},
           "hits_as_of": hits.get("ts"), "current_ladder": ladder(config_ladder(ctx.cfg["execution"])),
           "suggested": None, "reason": reco.get("reason") if cached else "no cached recommendation yet (made daily)"}
    if reco.get("ok"):
        out["suggested"] = {"ladder": ladder(reco["ladder"]), "expected": reco.get("expected"), "median": reco.get("median"),
                            "p_loss": reco.get("p_loss"), "first_hit": reco.get("first_hit"), "config": reco.get("config"),
                            "current_mean": (reco.get("current") or {}).get("mean"),
                            "follow_only_mean": (reco.get("follow_only") or {}).get("mean")}
    return out


# ---------------------------------------------------------------- summary
def build_summary(ctx):
    from .clusters import recent
    from .tgbot import ladder, position_value
    db, ex, cfg, t = ctx.db, ctx.executor, ctx.cfg, now()
    d0 = day_start_ts(cfg)
    f = db.one("SELECT COALESCE(SUM(pnl),0) AS total, COALESCE(SUM(CASE WHEN ts>=? THEN pnl ELSE 0 END),0) AS today "
               "FROM fills WHERE side='sell'", (d0,))
    c = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN sol_out>sol_in THEN 1 ELSE 0 END),0) AS wins, "
               "COALESCE(SUM(CASE WHEN closed_ts>=? THEN 1 ELSE 0 END),0) AS n_today FROM positions WHERE status='closed'", (d0,))
    n, wins = int(c["n"] or 0), int(c["wins"] or 0)
    opens, unreal = [], 0.0
    for p in ex.open_positions():
        v = position_value(p)
        unreal += v["unrealized"]
        opens.append({"id": p["id"], "symbol": p["symbol"] or short(p["mint"]), "mint": short(p["mint"]), "wallet": short(p["wallet"]),
                      "entry": p["entry_price"], "mult": v["mult"], "pnl_pct": v["pnl_pct"], "pnl_sol": v["pnl_sol"],
                      "age_s": t - (p["opened_ts"] or t), "left": p["tokens_left"] / p["tokens_init"] if p["tokens_init"] else 0,
                      "ladder": ladder(ex.ladder_of(p)) if p["exit_mode"] == "default" else f"mode: {p['exit_mode']}",
                      "tp_done": p["tp1_done"] or 0})
    closed = []
    for p in db.all("SELECT p.id, p.symbol, p.mint, p.wallet, p.opened_ts, p.closed_ts, p.sol_in, p.sol_out, p.max_mult, "
                    "(SELECT f.reason FROM fills f WHERE f.position_id=p.id AND f.side='sell' ORDER BY f.id DESC LIMIT 1) AS reason "
                    "FROM positions p WHERE p.status='closed' ORDER BY p.closed_ts DESC, p.id DESC LIMIT 20"):
        sin, sout = float(p["sol_in"] or 0), float(p["sol_out"] or 0)
        closed.append({"id": p["id"], "symbol": p["symbol"] or short(p["mint"]), "wallet": short(p["wallet"]),
                       "pnl_sol": sout - sin, "mult": sout / sin if sin else None, "max_mult": p["max_mult"],
                       "hold_s": (p["closed_ts"] or 0) - (p["opened_ts"] or 0), "closed_ts": p["closed_ts"], "reason": p["reason"]})
    from .deep import insider_set
    ins, wl = insider_set(db), []
    for r in db.all("SELECT address, status, score, stats FROM wallets WHERE status IN ('watch','qualified','dormant') "
                    "ORDER BY CASE WHEN status='watch' THEN 0 WHEN status='qualified' THEN 1 ELSE 2 END, score DESC LIMIT 50"):
        try:
            s = json.loads(r["stats"] or "{}")
        except ValueError:
            s = {}
        wl.append({"wallet": short(r["address"]), "insider": r["address"] in ins,
                   "status": {"watch": "watch", "qualified": "bench"}.get(r["status"], r["status"]),
                   "score": r["score"], "win_rate": s.get("win_rate"), "pnl_sol": s.get("pnl_sol"), "pnl_7d": s.get("pnl_7d"),
                   "closed": s.get("closed"), "loss_streak": s.get("loss_streak")})
    b = ctx.budget
    return {
        "ts": t,
        "status": {"mode": "paper", "paused": ex.paused(), "watcher": ctx.watcher.mode, "uptime_s": int(time.time() - ctx.started),
                   "credits_month": b.used(), "credits_budget": cfg["budget"]["helius_monthly_credits"],
                   "open": len(opens), "max_open": ex.c["max_open"], "spent_today": ex.spent_today(),
                   "daily_cap": ex.c["daily_cap_sol"], "last_deep": (db.kv_get("last_deep") or {}).get("ts"),
                   "insiders": len(ins)},
        "pnl": {"realized_total": float(f["total"] or 0), "realized_today": float(f["today"] or 0), "unrealized": unreal,
                "closed": n, "wins": wins, "win_rate": wins / n if n else None, "closed_today": int(c["n_today"] or 0)},
        "open_positions": opens, "closed_trades": closed, "watchlist": wl,
        "exits": _exits(ctx), "clusters": recent(db, 10), "settings": current_settings(ctx),
    }


# ---------------------------------------------------------------- settings + actions (writes)
FORM_KEYS = ("trade_sol", "stop_loss", "time_stop_h", "tp_ladder", "exit_mode", "cluster_window", "cluster_wallets",
             "cluster_age_h", "scoring_profile")


def current_settings(ctx):
    from .exits import config_ladder
    from .settings import _get, apply, overrides, trade_cap
    if not hasattr(ctx, "cfg_base"):
        apply(ctx)
    e, cl, sc = ctx.cfg["execution"], ctx.cfg.get("clusters") or {}, ctx.cfg["scoring"]
    lad = ",".join(f"{t['frac'] * 100:g}@{t['mult']:g}" for t in config_ladder(e))
    return {"trade_sol": float(e["per_trade_sol"]), "trade_cap": trade_cap(ctx.cfg_base),
            "stop_loss": round((1 - float(e["stop_loss_mult"])) * 100, 1), "time_stop_h": e["time_stop_h"], "tp_ladder": lad,
            "exit_mode": "follow" if e.get("default_exit") == "follow" else "tp",
            "cluster_window": cl.get("window_min"), "cluster_wallets": cl.get("min_wallets"), "cluster_age_h": cl.get("max_token_age_h"),
            "scoring_profile": sc.get("profile") or "strict", "overrides": sorted(overrides(ctx)), "mode": "paper"}


def save_settings(ctx, body):
    """-> (status, payload). All-or-nothing: every field is validated (settings.validate) before any is saved."""
    from .settings import ALIASES, FORBIDDEN, KNOBS, apply, set_value, validate
    if not isinstance(body, dict) or not body:
        return 400, {"ok": False, "error": "send a JSON object of settings"}
    for k, v in body.items():
        kl = str(k).strip().lower()
        if kl in FORBIDDEN or "live" in kl or kl.split(".")[-1] == "mode" or str(v).strip().lower() == "live":
            return 400, {"ok": False, "error": "Paper mode is forced. Live trading can't be enabled from the dashboard."}
    if not hasattr(ctx, "cfg_base"):
        apply(ctx)
    errors, todo = {}, []
    for k, v in body.items():
        key = ALIASES.get(str(k).strip().lower(), str(k).strip().lower())
        if key not in KNOBS:
            errors[k] = "unknown setting"; continue
        raw = str(v).strip()
        if raw == "":
            continue
        if raw.lower() not in ("default", "reset", "config"):
            try:
                validate(key, raw, ctx.cfg_base)
            except ValueError as e:
                errors[k] = str(e); continue
        todo.append((key, raw))
    if errors:
        return 400, {"ok": False, "errors": errors}
    saved = {}
    for key, raw in todo:
        ok, msg = set_value(ctx, key, raw)
        if not ok:  # validated above; keep going but report
            errors[key] = msg
        else:
            saved[key] = msg
    return (200 if not errors else 400), {"ok": not errors, "saved": saved, "errors": errors, "settings": current_settings(ctx)}


ACTIONS = ("pause", "resume", "scout", "deep")


def do_action(ctx, action):
    """-> (status, payload). scout / deep start in the background under ctx.scout_lock; results also go to Telegram."""
    db = ctx.db
    if action == "pause":
        db.kv_set("paused", True); return 200, {"ok": True, "paused": True, "message": "Paused: no new paper entries. Exits keep running."}
    if action == "resume":
        db.kv_set("paused", False); return 200, {"ok": True, "paused": False, "message": "Resumed: new paper entries are on."}
    if action not in ("scout", "deep"):
        return 400, {"ok": False, "error": f"unknown action; use one of {', '.join(ACTIONS)}"}
    if ctx.scout_lock.locked():
        return 409, {"ok": False, "error": "A scout is already running. Try again in a few minutes."}
    if not ctx.helius.key:
        return 400, {"ok": False, "error": "HELIUS_API_KEY is not set."}
    if ctx.budget.over_hard_stop():
        return 409, {"ok": False, "error": "Monthly Helius credit hard stop reached: scouting is paused."}

    async def job():
        try:
            if action == "scout":
                from .app import scout_job
                res = await scout_job(ctx)
                await ctx.tg.send(f"🔍 <b>Scout</b> (dashboard): {res.get('tokens', 0)} tokens, "
                                  f"{res.get('candidates', 0)} new candidates, {res.get('scored', 0)} scored")
            else:
                from .deep import format_result, run_deep
                async with ctx.scout_lock:
                    res = await run_deep(ctx, manual=True)
                await ctx.tg.send(format_result(res))
        except Exception as e:
            log.exception("dashboard %s failed: %s", action, e)
    st = ctx.__dict__.setdefault("_dash", {})
    st["action_task"] = asyncio.get_running_loop().create_task(job())
    return 202, {"ok": True, "started": action,
                 "message": "Scout started; the result lands in Telegram." if action == "scout"
                 else "Deep scout started (slow, credit-capped); the result lands in Telegram."}


def add_routes(app, ctx):
    cache = ctx.__dict__.setdefault("_dash", {}).setdefault("summary_cache", {"ts": 0.0, "body": None})

    async def _body(req):
        if not req.content_type.startswith("application/json"):
            return None
        try:
            return await req.json()
        except Exception:
            return None

    def _out(status, payload):
        cache["body"] = None  # next summary reflects the change
        return web.json_response(payload, status=status, headers=HDRS, dumps=lambda o: json.dumps(o, default=_json))

    async def post_settings(req):
        if not _authorized(ctx, req):
            return _deny()
        body = await _body(req)
        if body is None:
            return _out(400, {"ok": False, "error": "send application/json"})
        return _out(*save_settings(ctx, body))

    async def get_settings(req):
        if not _authorized(ctx, req):
            return _deny()
        return _out(200, current_settings(ctx))

    async def post_action(req):
        if not _authorized(ctx, req):
            return _deny()
        body = await _body(req)
        if not isinstance(body, dict):
            return _out(400, {"ok": False, "error": "send application/json {\"action\": ...}"})
        return _out(*do_action(ctx, str(body.get("action", "")).strip().lower()))

    async def summary(req):
        if not _authorized(ctx, req):
            return _deny()
        if cache["body"] is None or time.monotonic() - cache["ts"] > CACHE_S:
            cache["body"] = json.dumps(build_summary(ctx), default=_json, separators=(",", ":"))
            cache["ts"] = time.monotonic()
        return web.Response(text=cache["body"], content_type="application/json", headers=HDRS)

    async def page(req):
        if not _authorized(ctx, req):
            return _deny()
        resp = web.Response(text=PAGE, content_type="text/html", headers=HDRS)
        if req.query.get("key"):
            https = req.secure or req.headers.get("X-Forwarded-Proto", "") == "https"
            resp.set_cookie(COOKIE, req.query["key"], max_age=90 * 86400, httponly=True, secure=https, samesite="Lax",
                            path="/")
        return resp

    app.router.add_get("/dashboard", page)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    app.router.add_post("/api/action", post_action)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#0d1117"><title>Copy-trader</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#262c36;--fg:#e6edf3;--mute:#8b949e;--g:#3fb950;--r:#f85149;--y:#d29922;--b:#58a6ff}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:960px;margin:0 auto;padding:12px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:20px 2px 8px}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.pill{padding:3px 9px;border-radius:999px;background:var(--card);border:1px solid var(--line);font-size:12px;white-space:nowrap}
.paper{background:#1f2a44;border-color:#2f4a7a;color:#a5c8ff;font-weight:600}.run{color:var(--g)}.pause{color:var(--y)}
.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}@media(min-width:640px){.cards{grid-template-columns:repeat(4,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .k{font-size:12px;color:var(--mute)}.card .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:7px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}.l{text-align:left!important}
.pos{color:var(--g)}.neg{color:var(--r)}.mute{color:var(--mute)}.small{font-size:12px}
.empty{padding:12px;color:var(--mute)}a{color:var(--b);text-decoration:none}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;overflow-x:auto;font-size:12px;margin:8px 0 0}
#err{display:none;background:#3d1d20;border:1px solid #6e2b30;color:#ffb3b0;padding:8px 10px;border-radius:8px;margin-top:8px}
footer{color:var(--mute);font-size:12px;text-align:center;margin:18px 0 8px}
.btns{display:flex;flex-wrap:wrap;gap:8px}
button{font:inherit;color:var(--fg);background:#21262d;border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-height:44px;cursor:pointer}
button.pri{background:#238636;border-color:#2ea043;font-weight:600}button:disabled{opacity:.5}
.form{display:grid;grid-template-columns:1fr;gap:10px}@media(min-width:640px){.form{grid-template-columns:1fr 1fr}}
.form label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mute)}
.form input,.form select{font:16px/1.3 inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;min-height:44px;width:100%}
.msg{margin-top:8px;font-size:13px;min-height:1em}.ok{color:var(--g)}.bad{color:var(--r)}
</style></head><body><main>
<div class="bar" id="bar"><span class="pill paper">📄 PAPER</span><span class="pill mute">loading…</span></div>
<div id="err"></div>
<h2>Controls</h2><div class="card"><div class="btns">
<button id="b-pause" data-a="pause">⏸ Pause</button><button data-a="scout">🔍 Run scout</button><button data-a="deep">🔭 Run deep scout</button>
</div><div class="msg" id="amsg"></div></div>
<h2>Settings · 📄 paper only</h2><div class="card"><form id="sf" class="form" autocomplete="off">
<label>Trade size, SOL <span id="capnote"></span><input name="trade_sol" type="number" step="0.001" min="0.01" inputmode="decimal"></label>
<label>Stop loss, % loss<input name="stop_loss" type="number" step="1" min="5" max="95" inputmode="numeric"></label>
<label>Time stop, hours<input name="time_stop_h" type="number" step="1" min="1" max="168" inputmode="numeric"></label>
<label>TP ladder (pct@mult)<input name="tp_ladder" type="text" placeholder="50@2,50@3"></label>
<label>Exit mode<select name="exit_mode"><option value="tp">TP ladder</option><option value="follow">Follow wallet</option></select></label>
<label>Cluster window, min<input name="cluster_window" type="number" step="1" min="5" max="1440" inputmode="numeric"></label>
<label>Cluster min wallets<input name="cluster_wallets" type="number" step="1" min="2" max="25" inputmode="numeric"></label>
<label>Max token age, hours<input name="cluster_age_h" type="number" step="1" min="1" max="168" inputmode="numeric"></label>
<label>Scoring profile<select name="scoring_profile"><option value="relaxed">Relaxed (paper default)</option><option value="strict">Strict</option></select></label>
<div><button class="pri" type="submit" id="save" style="width:100%">Save settings</button></div>
</form><div class="msg" id="smsg"></div></div>
<h2>PnL</h2><div class="cards" id="cards"></div>
<h2>Open positions</h2><div class="wrap" id="open"></div>
<h2>Smart-money clusters</h2><div class="wrap" id="clusters"></div>
<h2>Closed trades · last 20</h2><div class="wrap" id="closed"></div>
<h2>Watchlist</h2><div class="wrap" id="wl"></div>
<h2>Exits</h2><div class="card" id="exits"></div>
<footer id="foot">Refreshes every 30 s</footer>
</main><script>
(function(){
var K=new URLSearchParams(location.search).get("key");
if(K&&history.replaceState)history.replaceState(null,"",location.pathname);
function $(id){return document.getElementById(id)}
function e(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function n(x,d){return x==null||isNaN(x)?"–":Number(x).toFixed(d)}
function sol(x){return x==null?"–":(x>0?"+":"")+n(x,3)}
function pc(x,s){return x==null?"–":(s&&x>0?"+":"")+n(x*100,1)+"%"}
function mx(x){return x==null?"–":n(x,2)+"x"}
function cls(x){return x>1e-9?"pos":x<-1e-9?"neg":""}
function dur(s){s=Math.max(0,s|0);return s<3600?Math.floor(s/60)+"m":s<172800?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d"}
function ago(ts){return ts?dur(Date.now()/1000-ts)+" ago":"–"}
function px(x){return x?Number(x).toExponential(2):"–"}
function usd(x){if(!x)return"–";x=+x;return x>=1e6?"$"+n(x/1e6,1)+"M":x>=1e3?"$"+n(x/1e3,1)+"k":"$"+n(x,0)}
function tbl(head,rows,empty){if(!rows.length)return'<div class="empty">'+e(empty)+'</div>';
 return"<table><thead><tr>"+head.map(function(h){return"<th>"+e(h)+"</th>"}).join("")+"</tr></thead><tbody>"+
 rows.map(function(r){return"<tr>"+r.map(function(c){return"<td"+(c&&c.c?' class="'+c.c+'"':"")+">"+(c&&c.h!=null?c.h:e(c&&c.t!=null?c.t:c))+"</td>"}).join("")+"</tr>"}).join("")+"</tbody></table>"}
function card(k,v,c){return'<div class="card"><div class="k">'+e(k)+'</div><div class="v '+(c||"")+'">'+e(v)+"</div></div>"}
function q(){return K?"?key="+encodeURIComponent(K):""}
var S0=null,dirty=false;
function fill(st){if(!st||dirty)return;S0=st;var f=$("sf");FK.forEach(function(k){if(f[k]!=null&&st[k]!=null)f[k].value=st[k]});
 $("capnote").textContent="(max "+n(st.trade_cap,3)+")";f.trade_sol.max=st.trade_cap}
var FK=["trade_sol","stop_loss","time_stop_h","tp_ladder","exit_mode","cluster_window","cluster_wallets","cluster_age_h","scoring_profile"];
function post(u,b){return fetch(u+q(),{method:"POST",credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})
 .then(function(r){if(r.status==401)throw new Error("Unauthorized: open /dashboard?key=… again");return r.json().then(function(j){j._s=r.status;return j})})}
function say(id,t,ok){var m=$(id);m.textContent=t;m.className="msg "+(ok?"ok":"bad")}
$("sf").addEventListener("input",function(){dirty=true});
$("sf").addEventListener("submit",function(ev){ev.preventDefault();var f=$("sf"),b={};
 FK.forEach(function(k){var v=String(f[k].value).trim();if(!S0||String(S0[k])!==v)b[k]=v});
 if(f.trade_sol.value&&S0&&+f.trade_sol.value>S0.trade_cap){say("smsg","Trade size is capped at "+n(S0.trade_cap,3)+" SOL",false);return}
 if(!Object.keys(b).length){say("smsg","Nothing changed",true);return}
 $("save").disabled=true;say("smsg","Saving…",true);
 post("/api/settings",b).then(function(j){if(j.ok){dirty=false;fill(j.settings);say("smsg","✅ Saved: "+Object.values(j.saved).join(" · "),true);load()}
  else say("smsg","⚠️ "+(j.error||Object.keys(j.errors||{}).map(function(k){return k+": "+j.errors[k]}).join(" · ")),false)})
 .catch(function(e){say("smsg",e.message,false)}).then(function(){$("save").disabled=false})});
Array.prototype.forEach.call(document.querySelectorAll("[data-a]"),function(btn){btn.addEventListener("click",function(){
 var a=btn.getAttribute("data-a");if((a=="scout"||a=="deep")&&!confirm(a=="deep"?"Run the deep scout? It is slow and spends Helius credits.":"Run the scout now? It spends Helius credits."))return;
 btn.disabled=true;post("/api/action",{action:a}).then(function(j){say("amsg",(j.ok?"✅ ":"⚠️ ")+(j.message||j.error||""),j.ok);if(j.ok)load()})
 .catch(function(e){say("amsg",e.message,false)}).then(function(){btn.disabled=false})})});
function render(d){
 var s=d.status,p=d.pnl,x=d.exits;fill(d.settings);
 var pb=$("b-pause");pb.setAttribute("data-a",s.paused?"resume":"pause");pb.textContent=s.paused?"▶️ Resume":"⏸ Pause";
 $("bar").innerHTML='<span class="pill paper">📄 PAPER</span>'+
  (s.paused?'<span class="pill pause">⏸ paused</span>':'<span class="pill run">▶ running</span>')+
  '<span class="pill">watcher: '+e(s.watcher)+'</span><span class="pill">up '+dur(s.uptime_s)+'</span>'+
  '<span class="pill">Helius '+Number(s.credits_month).toLocaleString()+" / "+Number(s.credits_budget).toLocaleString()+'</span>'+
  '<span class="pill">open '+s.open+"/"+s.max_open+" · spent "+n(s.spent_today,3)+"/"+n(s.daily_cap,3)+" SOL</span>"+(s.last_deep?'<span class="pill">deep scout '+ago(s.last_deep)+"</span>":"");
 $("cards").innerHTML=card("Realized total (SOL)",sol(p.realized_total),cls(p.realized_total))+
  card("Realized today (SOL)",sol(p.realized_today),cls(p.realized_today))+
  card("Win rate",pc(p.win_rate))+card("Closed trades",p.closed+(p.closed_today?" (+"+p.closed_today+" today)":""));
 $("open").innerHTML=tbl(["Token","Entry","Mult","PnL %","Age","TP ladder","Wallet"],d.open_positions.map(function(o){
  return["#"+o.id+" "+o.symbol,px(o.entry),mx(o.mult),{t:pc(o.pnl_pct,1),c:cls(o.pnl_pct)},dur(o.age_s),
   {t:o.ladder+(o.tp_done?" · hit "+o.tp_done:""),c:"l small"},{t:o.wallet,c:"mute"}]}),"No open positions");
 $("clusters").innerHTML=tbl(["Token","Wallets","SOL","Liq","Mcap","Seen",""],d.clusters.map(function(c){
  var u=String(c.dex_url||"");var ok=u.indexOf("https:")===0;
  return[{h:e(c.symbol)+' <span class="mute small">'+e(c.mint_short)+"</span>"},c.wallets,n(c.total_sol,3),usd(c.liq_usd),usd(c.mcap_usd),
   ago(c.last_ts),{h:ok?'<a href="'+e(u)+'" target="_blank" rel="noopener noreferrer">chart</a>':""}]}),"No clusters yet");
 $("closed").innerHTML=tbl(["Token","PnL SOL","Mult","Peak","Hold","Exit","Wallet"],d.closed_trades.map(function(c){
  return["#"+c.id+" "+c.symbol,{t:sol(c.pnl_sol),c:cls(c.pnl_sol)},mx(c.mult),mx(c.max_mult),dur(c.hold_s),{t:c.reason||"",c:"l small"},{t:c.wallet,c:"mute"}]}),"No closed trades yet");
 $("wl").innerHTML=tbl(["Wallet","Score","Win rate","PnL SOL","7d","Status"],d.watchlist.map(function(w){
  return[{h:(w.insider?"🎯 ":"")+e(w.wallet)},n(w.score,0),pc(w.win_rate),n(w.pnl_sol,3),{t:sol(w.pnl_7d),c:cls(w.pnl_7d)},{t:w.status,c:w.status=="watch"?"pos":"mute"}]}),"No wallets yet")+
  (s.insiders?'<div class="small mute" style="padding:6px 9px">🎯 insider-likely: early on 2+ winners, still active, ranked first</div>':"");
 var h='<div class="small mute">'+(x.as_of?"Daily recommendation, "+ago(x.as_of):"No cached recommendation yet")+"</div>";
 if(x.n){h+='<div style="margin-top:6px">'+e(x.n_wallet_trades)+" wallet + "+e(x.n_paper)+" paper trades · wallet exits p25 <b>"+mx(x.p25)+
  "</b> · median <b>"+mx(x.median)+"</b> · p75 <b>"+mx(x.p75)+"</b></div>";}
 if(x.levels.length){h+='<div class="wrap" style="margin-top:8px">'+tbl(["Level","Wallet hit","Our 24h peak"],x.levels.map(function(L){
  return["≥"+L+"x",pc(x.wallet_hit[L]),pc(x.our_hit[L])]}),"")+"</div>";}
 if(x.suggested){var g=x.suggested;h+='<div style="margin-top:10px">✅ Suggested: <b>'+e(g.ladder)+"</b></div>"+
  '<div class="small mute">sim '+mx(g.expected)+"/trade · median "+mx(g.median)+" · losers "+pc(g.p_loss)+" · 1st tranche hit "+pc(g.first_hit)+
  "<br>current "+e(x.current_ladder)+": "+mx(g.current_mean)+" · follow-only "+mx(g.follow_only_mean)+"</div><pre>"+e(g.config)+"</pre>";}
 else{h+='<div class="small" style="margin-top:8px">No ladder suggested'+(x.reason?": "+e(x.reason):"")+'</div><div class="small mute">Current: '+e(x.current_ladder)+"</div>";}
 $("exits").innerHTML=h;
 $("foot").textContent="Updated "+new Date(d.ts*1000).toLocaleTimeString()+" · refreshes every 30 s";
}
function load(){
 var u="/api/summary"+q();
 fetch(u,{credentials:"same-origin",cache:"no-store"}).then(function(r){
  if(r.status==401)throw new Error("Unauthorized: open /dashboard?key=… again");
  if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}).then(function(d){$("err").style.display="none";render(d)})
 .catch(function(err){var b=$("err");b.textContent=err.message;b.style.display="block"});
}
load();setInterval(function(){if(!document.hidden)load()},30000);
document.addEventListener("visibilitychange",function(){if(!document.hidden)load()});
})();
</script></body></html>"""
