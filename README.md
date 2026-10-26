# Solana Smart-Wallet Copy-Trader — v1 (PAPER)

Finds wallets that buy pumped memecoins early and profitably, watches them, and **paper-trades** their buys
with simulated fills at live Jupiter quotes. Rules follow `docs/architecture.md`. Free tools only:
Helius free plan, Jupiter keyless, Dexscreener, Telegram, Render free, any free Postgres.
**LIVE mode is a disabled stub. It never reads, stores, or uses private keys.**

## How it works (one process)

| Component | File | What it does |
|---|---|---|
| HTTP | `copytrader/app.py` | aiohttp on `$PORT`: `GET /health`, `POST /tg` (Telegram webhook), `POST /helius` (Helius raw webhook), `GET /dashboard` + `GET /api/summary` (read-only). Background loops for scout, exits and daily/weekly jobs. |
| Scout | `scout.py` | Every 2h: Dexscreener boosts/profiles → Solana tokens 1–72h old, +300% (h6/h24), liquidity ≥ $20k → Helius `getTransactionsForAddress` (oldest-first) on the mint → early buyers (first 30 min, skips the creator and launch-slot snipers) become **candidates**. |
| Scorer | `scorer.py` | Backfills 30 days of each candidate's swaps (gTFA, 500 txs = 50 credits), then applies the architecture rules: ≥20 closed trades, win rate ≥55% (win = +30%), PnL ≥50 SOL, median win ≥2x, top trade <50% of PnL, median entry ≤30 min after launch, not a sniper, median hold 10 min–24 h. It excludes insiders (tokens received without buying), bundlers (same slot + mint as other candidates), fast dumpers, wash traders, dev/creator wallets, deployer-funded wallets and shared-funder clusters. Passing wallets go to the **watchlist** (up to 25), then to a bench. Re-scored daily. A wallet is demoted after 5 straight losses, 7-day PnL < 0, or failing a rule. |
| Watcher | `watcher.py` | **Webhook mode** (default on Render): one Helius raw webhook on the watchlist plus wallets with open positions. It is re-synced only when that set changes (100 credits per edit). **Poll mode** (fallback, used locally): `getSignaturesForAddress` per wallet, with the interval stretched automatically to fit `poll_daily_credits`. |
| Paper executor | `executor.py` | 0.1 SOL per trade, daily cap 1 SOL, at most 5 open positions, 1 entry per token. Checks before entry: wallet buy ≥ 0.2 SOL, signal < 3 min old, liquidity ≥ $10k, mint and freeze authority revoked, Jupiter route with price impact ≤ 15%, our price ≤ 1.5x the wallet's entry, and an immediate sell quote that returns ≥ 70% (honeypot check). The fill is the Jupiter `outAmount` minus a 1% haircut and 0.0005 SOL in fees. |
| Exits | `executor.py` | Default exits run together: **follow** (when the wallet sells X%, we sell X% of what's left), **TP ladder** (default 50% at 2x, the rest at 3x; or any `execution.tp_ladder`; each position keeps the ladder it opened with), **stop-loss** at −50%, **time stop** at 24 h. Prices are checked every 30 s on Dexscreener and stored in `price_samples`; sells fill at a Jupiter quote. With no route, a sell is valued at the Dexscreener price minus 15%, or 0 if the token has no price. |
| Exit analytics | `exits.py` | From closed trades of watchlist wallets (and our closed paper trades): size-weighted realized exit multiples (partial sells count) with p25/median/p75, hit rates for ≥2x…100x, and the 24 h peak after **our** entry (wallet's first-buy price +15% by default). Prices come from every stored swap on the mint (lower bound on the peak) and the executor's own 30 s samples (`price_samples`). A recommender replays each trade through candidate TP ladders with the bot's real rules and suggests the best one as `execution.tp_ladder`. |
| Telegram | `tgbot.py` | HTML messages (bold headers, `<pre>` tables, 🟢 profit / 🔴 loss / ⏸ paused / 📄 PAPER / 🎯 insider-likely). 3–5 line buy/sell alerts, a daily PnL summary at 21:00 Lagos time, and commands. Only `TELEGRAM_CHAT_ID` is obeyed. Messages are clipped safely under 4096 chars; if Telegram rejects the markup the alert is resent as plain text. |
| Dashboard | `dashboard.py` | Read-only mobile page + JSON (see below). |
| Clusters | `clusters.py` | Smart-money cluster alert: several watchlist wallets buying the same new token (alert only, never buys). |
| Settings | `settings.py` | `/set` + `/settings`: DB overrides on top of `config.yaml` for a few knobs. Paper mode is forced. |
| Winners scout | `winners.py` | Daily: early buyers of big 1–30-day-old pumps who sold ≥ 3x on ≥ 2 of them and are still active → scorer. |
| Deep scout | `deep.py` | Weekly, 1-year lookback: early (pre-move) buyers on ≥ 2 winners → 🎯 `insider_likely` (active) or `dormant`. |
| Live | `live.py` | A stub that raises an error. With `mode: live` in config the app refuses to start. |

### Telegram commands
`/status` `/wallets` `/positions` `/pnl` `/clusters` `/exits [wallet]` `/settings` `/set <key> <value>` `/scout_winners [deep]` `/pause` `/resume` `/help`
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

## Dashboard (read-only)
Open `https://<your-service>.onrender.com/dashboard?key=<KEY>` on your phone. `<KEY>` is env `DASHBOARD_KEY` if you set one,
otherwise the existing `TG_WEBHOOK_SECRET` (Render → service → Environment), so no new env var is needed.
The first visit sets an HttpOnly cookie (90 days) and strips the key from the address bar; bookmark `/dashboard` afterwards.
A missing or wrong key gets 401 (constant-time compare). There are no write actions.
- One self-contained HTML page (inline CSS + vanilla JS, no CDNs, ~9 KB) that fetches `GET /api/summary` every 30 s
  (paused while the tab is hidden). `/api/summary` also accepts `?key=`.
- Sections: status bar (PAPER, running/paused, watcher mode, uptime, Helius credits this month, open/spent, last deep scout),
  PnL cards (realized total, today, win rate, closed trades), open positions, smart-money clusters (last 10), closed trades
  (last 20), watchlist + bench + dormant (🎯 = insider-likely) and the exit summary.
- Cheap: a handful of SQL queries, cached in-process for 10 s. Exit stats come from the cached daily recommendation; the hit
  rates are filled once per new recommendation in a background thread, never inside a request.

## Smart-money clusters (alert only)
When ≥ `clusters.min_wallets` (3) distinct **watchlist** wallets buy the same token within `window_min` (60) minutes, or
≥ `min_wallets_for_amount` (2) wallets buy ≥ `min_total_sol` (5) SOL combined, and the token's pair is younger than
`max_token_age_h` (24 h), one Telegram alert goes out: symbol, short mint, Dexscreener link, age, liquidity, mcap, wallet
count, total SOL and each wallet's short form, score and buy size. It re-alerts only when the wallet count grows by +2 or the
total SOL doubles. Uses the swaps the watcher already stores; the only extra call is one keyless Dexscreener lookup once the
wallet thresholds pass. `auto_paper_buy: false` is reserved; nothing is bought. `/clusters` lists the last 10.

## Settings from Telegram
`/settings` shows the knobs; `/set <key> <value>` stores an override in the DB (survives redeploys, beats `config.yaml`);
`/set <key> default` removes it. Keys: `trade_sol` (0.01 up to `execution.max_per_trade_sol` and the daily cap), `stop_loss`
(% loss, 5–95), `time_stop_h` (1–168), `tp_ladder` (e.g. `50@2,50@4`, 1–4 tranches, rising multiples, ≤ 100%),
`cluster_wallets` (2–25), `cluster_window` (5–1440 min), `cluster_sol` (0.1–10000), `cluster_age_h` (1–168). The dotted
config names (`clusters.min_wallets`, …) work too. `mode` is not settable: paper is forced and no command enables live trading.
A new TP ladder applies to new trades only.

## Winners scouts
**Daily (`winners.py`, `/scout_winners`):** Solana pairs 1–30 days old with mcap ≥ $1M and liquidity ≥ $50k, from free
Dexscreener boosts, profiles, `/latest/dex/search` and the normal scout's finds (≤ 20 new tokens a run, each once). Early buyers
come from the mint's oldest 200 txs (20 credits): bought within 60 min of the first tx or at ≤ 20% of today's price, creator
and launch-slot snipers skipped. Only wallets early on ≥ 2 winners get a history fetch (50 credits); they must have sold ≥ 3x
realized on ≥ 2 of them and have a swap in the last 7 days, then the normal scorer decides. Credits are metered as `winners`
(10% of the daily allowance).

**Deep (`deep.py`, `/scout_winners deep`, Sundays 04:00):** 1-year lookback. Tokens = `scout.seed_winners` (curated mints)
+ Dexscreener discoveries launched ≤ 365 days ago (mcap ≥ $10M, 24h volume ≥ $50k, clone pairs with liquidity ≈ mcap dropped).
Early buyers are sampled with `getTransactionsForAddress` **ascending inside a blockTime window** (7 slices of 100 txs in the
first 60 minutes, ~80 credits per token however big the token is), so nothing pages through millions of signatures. If the
plan refuses gTFA, it falls back to `getSignaturesForAddress` on the earliest pool / bonding curve, only when the launch is
reachable within `max_sig_pages` pages; bigger tokens are skipped and logged. Caps: 400 credits per token, 12,000 per run,
purpose `deep` (50% of that day's allowance) and the monthly hard stop.
Still excluded: the dev/creator, block-0/1 snipers and same-slot bundles (≥ 3 wallets). Wallets that bought **before the big
move** (≤ 25% of the later high) on ≥ 2 separate winners get one activity probe: active in the last 7 days → tagged 🎯
`insider_likely`, scored with the volume rules waived but every hard filter kept (sniper, insider transfers, bundler, dev,
deployer-funded, funder cluster, wash, fast-dump) and +100 score so they rank first; inactive → `dormant`, re-probed weekly.
🎯 shows in `/wallets`, the dashboard, buy alerts and cluster alerts.
All twelve current seeds launched more than 12 months ago (Dec 2022 – Jan 2025), so with `seed_max_age_days: 365` they are
skipped and logged; raise it (e.g. 1500) to scan them.

## Run locally
```bash
cd code
pip install -r requirements.txt
python -m tests.test_dryrun          # full mocked dry run, no keys or network needed (or: pytest -q)
python -m tests.test_exits           # exit analytics + ladder recommender + /exits + auto-tune, mocked
python -m tests.test_dashboard       # dashboard auth, /api/summary, cache, no external URLs
python -m tests.test_features        # clusters, /set, winners + deep scouts, 🎯 tagging, Telegram HTML
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
| Winners scout (daily) | 20 tokens × 20 + 8 wallets × ~70 | ~29,000 |
| Deep scout (weekly) | 12 tokens × ~80 + 10 probes + 10 wallets × ~70, capped 12,000/run | ~8,000–48,000 |
| **Total, webhook mode** | | **≈ 230,000–270,000 (~25%)** |
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
