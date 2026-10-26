"""Watcher: Helius raw webhook (1 credit/event) or budgeted polling (1 credit/poll/wallet + 1 per new tx)."""
import asyncio, hashlib, math

from .clients import BudgetExceeded
from .parse import keys_of, parse_swaps
from .scorer import store_swaps
from .util import log, now


class Watcher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = ctx.cfg["watch"]
        self.mode = "webhook" if self.cfg["mode"] == "webhook" and ctx.public_url else "poll"

    def addresses(self):
        rows = self.ctx.db.all("SELECT address FROM wallets WHERE status='watch' UNION "
                               "SELECT DISTINCT wallet AS address FROM positions WHERE status='open'")
        return sorted(r["address"] for r in rows)

    async def sync(self):
        """Push the watch set to the Helius webhook only when it changed (each edit = 100 credits)."""
        if self.mode != "webhook":
            return
        addrs = self.addresses()
        h = hashlib.sha1(",".join(addrs).encode()).hexdigest()
        if self.ctx.db.kv_get("webhook_hash") == h and self.ctx.db.kv_get("webhook_id"):
            return
        if not addrs:
            return
        try:
            wid = await self.ctx.helius.upsert_webhook(self.ctx.db.kv_get("webhook_id"), self.ctx.public_url + "/helius",
                                                       addrs, self.ctx.helius_secret)
            self.ctx.db.kv_set("webhook_id", wid); self.ctx.db.kv_set("webhook_hash", h)
            log.info("helius webhook %s synced: %d addresses", wid, len(addrs))
        except Exception as e:
            log.error("webhook sync failed: %s", e)

    async def disable_webhook(self):
        wid = self.ctx.db.kv_get("webhook_id")
        if wid:
            try:
                await self.ctx.helius.delete_webhook(wid)
            except Exception as e:
                log.error("webhook delete failed: %s", e)
            self.ctx.db.kv_set("webhook_id", None); self.ctx.db.kv_set("webhook_hash", None)

    async def handle_webhook(self, payload):
        txs = payload if isinstance(payload, list) else [payload]
        self.ctx.budget.charge(len(txs), "watch")  # Helius bills 1 credit per delivered event
        for tx in txs:
            await self.process_tx(tx)

    async def process_tx(self, tx):
        watched = set(self.addresses())
        for wallet in watched.intersection(keys_of(tx)):
            for s in store_swaps(self.ctx.db, parse_swaps(tx, owner=wallet)):  # dedup: only new rows signal
                if s["side"] in ("buy", "sell"):
                    try:
                        await self.ctx.executor.on_signal(s)
                    except Exception as e:
                        log.exception("signal failed: %s", e)

    def poll_interval(self):
        n = max(len(self.addresses()), 1)
        return max(self.cfg["min_poll_interval_s"], math.ceil(n * 86400 / self.cfg["poll_daily_credits"]))

    async def poll_once(self):
        for addr in self.addresses():
            w = self.ctx.db.one("SELECT last_sig FROM wallets WHERE address=?", (addr,)) or {}
            try:
                sigs = await self.ctx.helius.get_signatures(addr, until=w.get("last_sig"), limit=20)
            except BudgetExceeded:
                return
            sigs = [x for x in sigs if not x.get("err")]
            if sigs:
                self.ctx.db.x("UPDATE wallets SET last_sig=? WHERE address=?", (sigs[0]["signature"], addr))
            if not w.get("last_sig"):
                continue  # first poll only sets the cursor
            for x in reversed(sigs):
                if now() - (x.get("blockTime") or now()) > self.cfg["max_signal_age_s"] * 5:
                    continue
                tx = await self.ctx.helius.get_transaction(x["signature"])
                if tx:
                    await self.process_tx(tx)

    async def poll_loop(self):
        while True:
            try:
                if self.mode == "poll":
                    await self.poll_once()
            except Exception as e:
                log.exception("poll error: %s", e)
            await asyncio.sleep(self.poll_interval())
