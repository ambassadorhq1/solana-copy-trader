# Solana Smart-Wallet Copy-Trader — v1 (PAPER)

Finds wallets that buy pumped memecoins early and profitably, watches them, and **paper-trades** their buys
with simulated fills at live Jupiter quotes. Rules follow `docs/architecture.md`. Free tools only:
Helius free plan, Jupiter keyless, Dexscreener, Telegram, Render free, any free Postgres.
**LIVE mode is a disabled stub. It never reads, stores, or uses private keys.**

## How it works (one process)

| Component | File | What it does |
|---|---|---|
| HTTP | `copytrader/app.py` | aiohttp on `$PORT`: `GET /health`, `POST /tg` (Telegram webhook), `POST /helius` (Helius raw webhook). Background loops for scout, exits and daily jobs. |
| Scout | `scout.py` | Every 2h: Dexscreener boosts/profiles → Solana tokens 1–72h old, +300% (h6/h24), liquidity ≥ $20k → Helius `getTransactionsForAddress` (oldest-first) on the mint → early buyers (first 30 min, skips the creator and launch-slot snipers) become **candidates**. |
| Scorer | `scorer.py` | Backfills 30 days of each candidate's swaps (gTFA, 500 txs = 50 credits), then applies the architecture rules: ≥20 closed trades, win rate ≥55% (win = +30%), PnL ≥50 SOL, median win ≥2x, top trade <50% of PnL, median entry ≤30 min after launch, not a sniper, median hold 10 min–24 h. It excludes insiders (tokens received without buying), bundlers (same slot + mint as other candidates), fast dumpers, wash traders, dev/creator wallets, deployer-funded wallets and shared-funder clusters. Passing wallets go to the **watchlist** (up to 25), then to a bench. Re-scored daily. A wallet is demoted after 5 straight losses, 7-day PnL < 0, or failing a rule. |
| Watcher | `watcher.py` | **Webhook mode** (default on Render): one Helius raw webhook on the watchlist plus wallets with open positions. It is re-synced only when that set changes (100 credits per edit). **Poll mode** (fallback, used locally): `getSignaturesForAddress` per wallet, with the interval stretched automatically to fit `poll_daily_credits`. |
| Paper executor | `executor.py` | 0.1 SOL per trade, daily cap 1 SOL, at most 5 open positions, 1 entry per token. Checks before entry: wallet buy ≥ 0.2 SOL, signal < 3 min old, liquidity ≥ $10k, mint and freeze authority revoked, Jupiter route with price impact ≤ 15%, our price ≤ 1.5x the wallet's entry, and an immediate sell quote that returns ≥ 70% (honeypot check). The fill is the Jupiter `outAmount` minus a 1% haircut and 0.0005 SOL in fees. |
| Exits | `executor.py` | Default exits run together: **follow** (when the wallet sells X%, we sell X% of what's left), **TP ladder** (default 50% at 2x, the rest at 3x; or any `execution.tp_ladder`; each position keeps the ladder it opened with), **stop-loss** at −50%, **time stop** at 24 h. Prices are checked every 30 s on Dexscreener and stored in `price_samples`; sells fill at a Jupiter quote. With no route, a sell is valued at the Dexscreener price minus 15%, or 0 if the token has no price. |
| Exit analytics | `exits.py` | From closed trades of watchlist wallets (and our closed paper trades): size-weighted realized exit multiples (partial sells count) with p25/median/p75, hit rates for ≥2x…100x, and the 24 h peak after **our** entry (wallet's first-buy price +15% by default). Prices come from every stored swap on the mint (lower bound on the peak) and the executor's own 30 s samples (`price_samples`). A recommender replays each trade through candidate TP ladders with the bot's real rules and suggests the best one as `execution.tp_ladder`. |
| Telegram | `tgbot.py` | Alerts on each paper buy and sell, a daily PnL summary at 21:00 Lagos time, and commands. Only `TELEGRAM_CHAT_ID` is obeyed. |
| Live | `live.py` | A stub that raises an error. With `mode: live` in config the app refuses to start. |

### Telegram commands
`/status` `/wallets` `/positions` `/pnl` `/exits [wallet]` `/pause` `/resume` `/help`
`/exits` shows the exit table and suggested ladder for the watchlist; `/exits <address or prefix>` for one wallet.
The daily summary ends with a one-line exit summary.
`/exit <id> follow|2x|3x|now` sets the exit for one trade:
- `follow`: sell only when the wallet sells (no TP ladder)
- `2x` / `3x`: sell the whole position at that multiple
- `now`: sell immediately

Stop-loss and the 24 h time stop always stay on. `/pause` stops new entries; exits keep running.

### How the TP ladder is recommended (`exits.py`)
1. Each closed trade becomes a case: our entry = wallet first-buy price × (1 + `exits.entry_delay_slippage`), or our real
   entry for closed paper trades. Its price path is every stored swap on the mint plus our 30 s samples for the next 24 h.
2. Candidate ladders: 1–3 tranches from `ladder_levels` × `ladder_fracs` (fractions of the initial size; a sum < 1 leaves a runner).
3. Each ladder is replayed on every case in time order: TP tranches, follow-the-wallet sells, −50% stop-loss, 24 h time stop,
   1% slippage + fee per sell. The score is the mean realized multiple.
4. Safety filters: the first tranche must be reached by ≥ `min_first_hit` (60%) of cases after our entry; every level used must be
   reached by ≥ `min_level_hits` cases; no suggestion with fewer than `min_cases` cases.
5. `exits.auto_tune_exits: true` applies the cached suggestion to **new** paper trades only, after ≥ 30 closed paper trades.
   Each position stores the ladder it opened with.

Limits: swap prices are sparse, so peaks are lower bounds and stop-loss timing is approximate. The simulation assumes we sell
at the wallet's price when following, which flatters runners. Treat the suggestion as a starting point and check it against paper results.

## Run locally
```bash
cd code
pip install -r requirements.txt
python -m tests.test_dryrun          # full mocked dry run, no keys or network needed (or: pytest -q)
python -m tests.test_exits           # exit analytics + ladder recommender + /exits + auto-tune, mocked
HELIUS_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python main.py   # SQLite + polling
```
Without `PUBLIC_URL`, the app uses Telegram long polling and Helius polling. All tunables are in `config.yaml`.

## Deploy (Render free + cron-job.org)
1. **Telegram:** create a bot with @BotFather and copy the token. Send the bot a message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`. That is `TELEGRAM_CHAT_ID`.
2. **Helius:** at dashboard.helius.dev (free plan), copy the API key.
3. **Postgres (free, persistent):** create a free Neon or Supabase database and copy the connection string,
   e.g. `postgresql://user:pass@host/db?sslmode=require`. Don't use Render's free Postgres: it expires 30 days after creation.
   Render's disk is ephemeral, so SQLite on Render loses everything on restart or spin-down.
4. **GitHub:** push the *contents* of this `code/` folder as the repo root (`render.yaml` must sit at the root).
5. **Render:** New → Blueprint → pick the repo. Fill in `HELIUS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`.
   The two webhook secrets are generated for you. Render sets `RENDER_EXTERNAL_URL`. On boot, the app registers
   the Telegram webhook at `/tg` and creates the Helius raw webhook at `/helius` once the watchlist has wallets.
6. **Keep it awake:** on cron-job.org (free), create a job that GETs `https://<your-service>.onrender.com/health`
   **every 10 minutes**. Free services sleep after 15 minutes without inbound HTTP. One always-on service uses about 744 of the
   750 free instance hours a month, so don't run a second free service in the same workspace.
7. Send `/status` to the bot. The first scout runs about a minute after boot.

## Helius free-plan credit budget (1M/month; the app meters itself against 900k)
Costs come from Helius docs: gTFA full is 10 credits per 100 txs returned (minimum 10), standard RPC is 1,
a webhook event is 1, and creating or editing a webhook is 100.

| Item | Assumption | Credits/month |
|---|---|---|
| Scout: early txs of pumped mints | 12 runs/day × 5 tokens × 20 | 36,000 |
| Candidate backfill (30d) | 12 × 6 wallets × 50 | 108,000 |
| Funder lookup (passing wallets only) | about 3/day × 10 | ~1,000 |
| Daily rescore (incremental) | 25 watch wallets × 10 | 7,500 |
| Webhook events | 25 wallets × ~40 tx/day × 1 | ~30,000 |
| Webhook edits | ~3/day × 100 | ~9,000 |
| Executor mint checks | ~1 per signal | ~1,000 |
| **Total, webhook mode** | | **≈ 190,000 (~19%)** |
| Poll-mode alternative | 12,000/day cap | +360,000 instead of webhook events |

Guards: scout plus backfill may use at most 35% of each day's allowance, and rescore at most 10%. At 97% of the monthly
budget, the app stops scouting and watching, deletes the webhook, and sends a Telegram alert; exits keep running because they don't use Helius.
Webhook volume is the main unknown: raw webhooks also deliver spam and dust transfers to watched wallets.
Check the Helius dashboard during the first week. `/status` shows the app's own count.

## Known limits (v1)
- **Not proven against real Helius data yet.** The dry run uses mocked txs. Dexscreener and the keyless Jupiter quote were called live once and worked. Postgres code paths were not run against a real server; only SQLite was exercised.
- If Helius refuses `getTransactionsForAddress` on the free plan, the app switches to `getSignaturesForAddress` + `getTransaction` (1 credit each, more calls). Early-buyer scans on busy mints get costly in that mode.
- Swap parsing uses balance changes, not instruction decoding. It covers SOL/WSOL-quoted swaps on any DEX or aggregator. It skips USDC/USDT-quoted swaps, token→token routes, and swaps where the watched wallet isn't the token owner (some bots use proxy accounts).
- Every gTFA page is capped at 500 txs. For very active wallets, the 30-day stats cover only the newest 500 txs.
- Scoring thresholds are the strict v1 defaults. Expect few wallets to pass at first; tune `config.yaml` against what the bench shows.
- Funder clustering only sees wallets this app has scored. Bundle detection only sees buys stored by this app.
- Paper fills are optimistic. Live copies land after the wallet: webhook latency is 1–3 s, plus a cold start of about 1 minute if the service slept. Jupiter keyless (0.5 RPS) adds about 4–5 s per entry for two quotes. Followers can be exit liquidity.
- Price checks run every 30 s on Dexscreener. Wicks between checks are missed, and stop-loss fills can be far below −50% on rugs.
- Telegram and Helius secrets are checked on every webhook call. Helius auto-disables a free-plan webhook with ≥95% failures in 24 h, so keep the cron ping running.
- Render may suspend free services with unusually high outbound traffic. The defaults are modest.
