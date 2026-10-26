"""HTTP clients: Helius (RPC + getTransactionsForAddress + webhooks), Jupiter keyless quotes, Dexscreener.
All throttled; Helius calls are metered against the monthly credit budget."""
import asyncio, calendar, json, math, time
from datetime import datetime

import aiohttp

from .util import RateLimiter, WSOL, log, local_day, tz


class BudgetExceeded(Exception):
    pass


class HeliusError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Budget:
    """Credit meter. Scout/score purposes get a share of the daily allowance; watch/exec only stop at the monthly hard stop."""

    def __init__(self, db, cfg):
        self.db, self.cfg, self.b = db, cfg, cfg["budget"]

    def _month(self):
        return local_day(self.cfg)[:7]

    def used(self, day=None, purpose=None):
        q, p = "SELECT COALESCE(SUM(used),0) AS u FROM credits WHERE day LIKE ?", [(day or self._month()) + "%"]
        if purpose:
            q += " AND purpose=?"; p.append(purpose)
        return int(self.db.val(q, p, 0))

    def daily_allowance(self):
        d = datetime.now(tz(self.cfg))
        days = calendar.monthrange(d.year, d.month)[1]
        return self.b["helius_monthly_credits"] / days

    def over_hard_stop(self, extra=0):
        return self.used() + extra > self.b["helius_monthly_credits"] * self.b["hard_stop_frac"]

    def allow(self, cost, purpose):
        if self.over_hard_stop(cost):
            return False
        share = self.b.get(f"{purpose}_share")
        if share is not None:
            return self.used(local_day(self.cfg), purpose) + cost <= self.daily_allowance() * share
        return True

    def charge(self, n, purpose):
        if n:
            self.db.x("INSERT INTO credits (day, purpose, used) VALUES (?, ?, ?) ON CONFLICT (day, purpose) "
                      "DO UPDATE SET used = credits.used + excluded.used", (local_day(self.cfg), purpose, int(n)))


class Http:
    def __init__(self):
        self.session = None

    async def s(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


class Helius:
    def __init__(self, api_key, cfg, budget, http):
        self.key, self.cfg, self.budget, self.http = api_key, cfg, budget, http
        self.url = f"{cfg['endpoints']['helius_rpc']}?api-key={api_key}"
        self.lim = RateLimiter(cfg["rate_limits"]["helius_rps"])
        self.gtfa_ok = True  # flips to False if the method is refused on this plan -> 1-credit fallbacks

    async def rpc(self, method, params, cost=1, purpose="watch"):
        if cost and not self.budget.allow(cost, purpose):
            raise BudgetExceeded(f"{purpose} budget: {method}")
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(4):
            await self.lim.wait()
            s = await self.http.s()
            async with s.post(self.url, json=body) as r:
                if r.status == 429:
                    await asyncio.sleep(1 + attempt * 2); continue
                txt = await r.text()
            try:
                j = json.loads(txt)
            except ValueError:
                raise HeliusError(f"HTTP {r.status}: {txt[:120]}", r.status)
            if "error" in j:
                e = j["error"]
                raise HeliusError(e.get("message", str(e)), e.get("code"))
            self.budget.charge(cost, purpose)
            return j.get("result")
        raise HeliusError("rate limited")

    async def gtfa(self, address, purpose, *, asc=False, limit=100, since=None, until=None, pages=1):
        """getTransactionsForAddress, full txs. 10 credits per 100 returned (min 10)."""
        out, tok = [], None
        for _ in range(pages):
            filters = {"status": "succeeded"}
            bt = {k: v for k, v in (("gte", since), ("lt", until)) if v}
            if bt:
                filters["blockTime"] = bt
            opts = {"transactionDetails": "full", "sortOrder": "asc" if asc else "desc", "limit": limit,
                    "encoding": "json", "maxSupportedTransactionVersion": 1, "filters": filters}
            if tok:
                opts["paginationToken"] = tok
            est = 10 * math.ceil(limit / 100)
            if not self.budget.allow(est, purpose):
                raise BudgetExceeded(f"{purpose} budget: gTFA")
            res = await self.rpc("getTransactionsForAddress", [address, opts], cost=0, purpose=purpose)
            data = (res or {}).get("data") or []
            self.budget.charge(max(10, 10 * math.ceil(len(data) / 100)), purpose)
            out += data
            tok = (res or {}).get("paginationToken")
            if not tok or len(data) < limit:
                break
        return out

    async def history(self, address, purpose, *, asc=False, limit=100, since=None, pages=1):
        if self.gtfa_ok:
            try:
                return await self.gtfa(address, purpose, asc=asc, limit=limit, since=since, pages=pages)
            except HeliusError as e:
                m = str(e).lower()
                if e.code in (-32601, -32403, 403) or any(w in m for w in ("not available", "not supported", "upgrade", "plan")):
                    log.warning("getTransactionsForAddress refused (%s); using getSignaturesForAddress fallback", e)
                    self.gtfa_ok = False
                else:
                    raise
        return await self._history_fallback(address, purpose, asc=asc, limit=limit * pages, since=since)

    async def _history_fallback(self, address, purpose, *, asc, limit, since):
        sigs, before = [], None
        for _ in range(30 if asc else 5):
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            page = await self.rpc("getSignaturesForAddress", [address, opts], 1, purpose) or []
            page = [p for p in page if not p.get("err")]
            sigs += page
            if len(page) < 1000 or (since and page and (page[-1].get("blockTime") or 0) < since) or (not asc and len(sigs) >= limit):
                break
            before = page[-1]["signature"]
        if since:
            sigs = [s for s in sigs if (s.get("blockTime") or 0) >= since]
        sigs = sigs[::-1][:limit] if asc else sigs[:limit]
        out = []
        for s in sigs:
            tx = await self.get_transaction(s["signature"], purpose)
            if tx:
                out.append(tx)
        return out

    async def get_signatures(self, address, until=None, limit=20, purpose="watch"):
        opts = {"limit": limit}
        if until:
            opts["until"] = until
        return await self.rpc("getSignaturesForAddress", [address, opts], 1, purpose) or []

    async def get_transaction(self, sig, purpose="watch"):
        try:
            return await self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}], 1, purpose)
        except HeliusError as e:
            if "version" in str(e).lower():
                return await self.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}], 1, purpose)
            raise

    async def mint_info(self, mint, purpose="exec"):
        r = await self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], 1, purpose)
        try:
            return r["value"]["data"]["parsed"]["info"]
        except (TypeError, KeyError):
            return None

    # --- webhooks (100 credits per create/edit/delete) ---
    async def _wh(self, method, path="", body=None):
        if not self.budget.allow(100, "watch"):
            raise BudgetExceeded("webhook management")
        await self.lim.wait()
        s = await self.http.s()
        url = f"{self.cfg['endpoints']['helius_webhooks']}{path}?api-key={self.key}"
        async with s.request(method, url, json=body) as r:
            txt = await r.text()
            if r.status >= 300:
                raise HeliusError(f"webhook {method} {r.status}: {txt[:200]}", r.status)
        self.budget.charge(100, "watch")
        return json.loads(txt) if txt.strip() else {}

    async def upsert_webhook(self, webhook_id, url, addresses, auth):
        body = {"webhookURL": url, "transactionTypes": ["ANY"], "accountAddresses": addresses,
                "webhookType": "raw", "authHeader": auth, "txnStatus": "success"}
        if webhook_id:
            try:
                await self._wh("PUT", f"/{webhook_id}", body)
                return webhook_id
            except HeliusError as e:
                if e.code != 404:
                    raise
        return (await self._wh("POST", "", body)).get("webhookID")

    async def delete_webhook(self, webhook_id):
        await self._wh("DELETE", f"/{webhook_id}")


class Jupiter:
    """Keyless quotes at 0.5 RPS (sliding 60s window). Optional JUP_API_KEY (free key = 1 RPS)."""

    def __init__(self, cfg, http, api_key=None):
        self.cfg, self.http, self.key = cfg, http, api_key
        self.lim = RateLimiter(cfg["rate_limits"]["jupiter_rps"])

    async def quote(self, input_mint, output_mint, amount, slippage_bps):
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                  "slippageBps": str(slippage_bps), "restrictIntermediateTokens": "true"}
        headers = {"x-api-key": self.key} if self.key else {}
        for attempt in range(3):
            await self.lim.wait()
            s = await self.http.s()
            try:
                async with s.get(self.cfg["endpoints"]["jupiter_quote"], params=params, headers=headers) as r:
                    if r.status == 429:
                        reset = r.headers.get("x-ratelimit-reset")
                        wait = max(1.0, float(reset) - time.time()) if reset else 2.0 * (attempt + 1)
                        self.lim.push_back(wait); continue
                    j = await r.json(content_type=None)
                    if r.status != 200 or "outAmount" not in j:
                        log.info("jupiter no route %s->%s: %s", input_mint[:6], output_mint[:6], str(j)[:150])
                        return None
                    return j
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("jupiter error %s", e)
        return None


class Dexscreener:
    def __init__(self, cfg, http):
        self.cfg, self.http = cfg, http
        self.base = cfg["endpoints"]["dexscreener"]
        self.lim = RateLimiter(cfg["rate_limits"]["dexscreener_rps"])
        self._sol_usd = (0.0, 0)

    async def get(self, path):
        for attempt in range(3):
            await self.lim.wait()
            s = await self.http.s()
            try:
                async with s.get(self.base + path) as r:
                    if r.status == 429:
                        self.lim.push_back(5 * (attempt + 1)); continue
                    if r.status != 200:
                        return None
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("dexscreener %s: %s", path, e)
        return None

    async def discover_mints(self):
        mints = []
        for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
            for it in (await self.get(path) or []):
                if it.get("chainId") == "solana" and it.get("tokenAddress") not in mints:
                    mints.append(it["tokenAddress"])
        return mints

    async def best_pairs(self, mints):
        """{mint: highest-liquidity Solana pair where the mint is the base token}"""
        out = {}
        mints = list(dict.fromkeys(mints))
        for i in range(0, len(mints), 30):
            for p in (await self.get("/tokens/v1/solana/" + ",".join(mints[i:i + 30])) or []):
                m = (p.get("baseToken") or {}).get("address")
                if m in mints and (m not in out or (p.get("liquidity") or {}).get("usd", 0) > (out[m].get("liquidity") or {}).get("usd", 0)):
                    out[m] = p
        return out

    async def sol_usd(self):
        val, ts = self._sol_usd
        if time.time() - ts < 300 and val:
            return val
        p = (await self.best_pairs([WSOL])).get(WSOL)
        if p and p.get("priceUsd"):
            self._sol_usd = (float(p["priceUsd"]), time.time())
        return self._sol_usd[0]

    async def price_sol(self, pair):
        """Price of 1 UI token in SOL."""
        if not pair:
            return None
        if (pair.get("quoteToken") or {}).get("address") == WSOL and pair.get("priceNative"):
            return float(pair["priceNative"])
        su = await self.sol_usd()
        return float(pair["priceUsd"]) / su if pair.get("priceUsd") and su else None
