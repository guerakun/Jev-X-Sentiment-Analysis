---
name: "jev-sentiment"
description: "Run Jev X sentiment analysis for a watched asset: crypto (BTC, ETH, SOL, HYPE, NEAR) via Kraken market data, or stocks/ETFs (SMH, DRAM, COIN, HOOD, BE) with caller-supplied price. Live tweets via twitterapi.io, decision via TypeSafe Jev model. Use for the twice-daily price watch sentiment read."
---

# Jev Sentiment

## Purpose
Produce a Jev X sentiment read for one symbol: live tweet sample, deterministic social stats, and a TypeSafe Jev System One decision (action, sentiment, squeeze risk, catalyst). Backs the twice-daily price watch.
- Crypto (BTC, ETH, SOL, HYPE, NEAR): market data from Kraken; tweets searched by cashtag OR full name.
- Stocks/ETFs (SMH, DRAM, COIN, HOOD, BE): caller passes `--price` and `--change-pct` (Kraken has no stock pairs; RSI/funding default to neutral); tweets searched by cashtag only (e.g. `$DRAM` — plain "DRAM" is memory-chip noise).

## Tooling
CLI: `~/workspace/skills/jev-sentiment/bin/jev_analyze.py`

Run with the Jev venv python (stdlib only plus the credential surrogate helper):
```
~/workspace/market-sentiment/jev-x/venv/bin/python ~/workspace/skills/jev-sentiment/bin/jev_analyze.py BTC --tweets 50
```
Output is a JSON decision object on stdout. Exit non-zero means failure; never trust partial output.

Stocks/ETFs (weekdays only — markets are closed on weekends):
```
~/workspace/market-sentiment/jev-x/venv/bin/python ~/workspace/skills/jev-sentiment/bin/jev_analyze.py COIN --tweets 50 --price 194.25 --change-pct 11.66
```
Get the price and % change from the regular price lookup first, then pass them in. The CLI searches X by cashtag only for stocks. `market.source` is `"kraken"` for crypto and `"provided"` for stocks; both are real data (`is_fallback: false`).

Auth uses the stored connectors via the surrogate helper; no raw keys anywhere:
- tweets: `custom.twitterapi-io` (X-API-Key header, api.twitterapi.io)
- Jev model: `custom.typesafe-ai` (Bearer header, api.typesafe.ai)
- market data: public Kraken REST via urllib, no key

Seen-tweet IDs persist in `~/workspace/skills/jev-sentiment/data/seen_ids.json` as passive bookkeeping only. Early-stopping dedup is driven solely by tweet IDs that have full bodies stored in `tweets_store.json`, so legacy IDs never suppress fresh fetches.
Normalized historical tweets persist in `~/workspace/skills/jev-sentiment/data/tweets_store.json` (up to 2000 per symbol, newest first). When early stopping fires, the sample is topped up from this store so a run never collapses to zero tweets — mirroring the upstream repo's "new tweets + historical database" behavior.

## Auth
Credentials are already stored; nothing here collects one. Never ask the user to paste a raw key, set a secret env var, or write an auth file.

A 401/403 is a question about the request before the key: verify the surrogate helper was used. Only after a credentialed request is still rejected, call `credentials.request_api_access` with `reconnect` for that provider.

## Operating Rules
1. The CLI refuses mock/simulated data: any failed external call exits non-zero. Never present mock output as a real read.
2. Default 50 tweets per symbol. Keep twice-daily runs at 50.
3. If the CLI fails during a price watch, report prices normally and note "sentiment read unavailable" rather than inventing a signal.
4. Present Jev output as decision support, not financial advice and never as an instruction to trade.
5. twitterapi.io is credit-based (15 credits per returned tweet, 15 minimum per call; free tier starts at $0.10 = 10,000 credits). If the CLI exits with `HTTP Error 402: Payment Required`, the account's credits are exhausted — sentiment reads cannot run until the balance is topped up at https://twitterapi.io/dashboard. The CLI keeps exiting non-zero in this state; it never falls back to mock data. (402 observed 2026-09-20 ~18:45 EDT after ~12 bootstrap runs burned through the balance.)
6. Bootstrap flag: `jev_analyze.py SYM --tweets 100 --no-early-stop` disables first-known-ID early stopping and paginates past stored tweets to build the historical store. Use only when bootstrapping; it costs more API pages. Normal twice-daily runs omit it.

## v1.1 (dev branch)

`market-brief/jev_analyze.py` on the `dev` branch adds:
- Author priors (`data/author_priors.json`): per-author last-10 baselines per ticker; posts scored as deltas; `sarcasm_flag` at |delta| >= 0.6.
- Quote-tweets detected and split out; empty-commentary quotes treated as endorsements (scored from author baseline).
- Per-call template in output `v1_1.calls[]`: tweet link, timestamp, author, polarity, baseline, delta, flags.
- Per-verdict falsifier ("This would change my mind: ___") in `v1_1.falsifier`.
- Hit-rate log `data/hit_rate.jsonl` + `--settle` mode (`--settle-prices` for stocks/ETFs). Misses labeled in place.
- Identical for crypto and stocks/ETFs; stock prices still via `--price` from free public quotes.
- Outside the Hatch runtime, creds fall back to `TWITTERAPI_IO_KEY` / `TYPESAFE_API_KEY` env vars.

## v1.2a (dev branch)

- Pine research layer: `research/research.pine` (ATR-14, Supertrend, confirmed 5/5 pivots, swing high/low, CMF-20, RSI-14, BOS flags) runs through LuxAlgo **pinets-cli as a separate OS process** (`.pine` in, JSON out on stdout; AGPL-3.0 boundary — never import pinets into the CLI). See `research/README.md`.
- Crypto: Kraken 4h OHLC (forming candle dropped) → research features as **context** in the Jev state (`market.research`: atr_pct, trend, cmf_20, rsi, bos — max 5 features), never standalone signals.
- Stocks/ETFs: real daily RSI-14 from Yahoo Finance chart bars (keyless), replacing the hardcoded 50.0. Last bar dropped only when it is today's still-forming session.
- `build_levels(action, price, research)`: volatility-adaptive when research is present — stop = 1.5x ATR, T1 = 1.5R, T2 = swing structure in the trade direction or 3R; `levels_basis` records `atr_1.5x_4h` / `atr_1.5x_1d`. Falls back to v1.1 fixed percentages (`fixed_v1.1`) when the layer degrades.
- Env overrides: `JEV_PINETS_CLI`, `JEV_RESEARCH_PINE`.
- Deploy: copy `market-brief/jev_analyze.py` → `~/workspace/skills/jev-sentiment/bin/jev_analyze.py` and re-apply the DATA_DIR patch (`SCRIPT_DIR/../data`); copy `research/research.pine` → `~/workspace/skills/jev-sentiment/research/research.pine`.

## v1.2b (dev branch)

- Real keyless funding rates for crypto (BTC, ETH, SOL, HYPE, NEAR): `fetch_funding_rate()` tries Binance `/fapi/v1/fundingRate` (official, keyless; geo-blocked from this egress with HTTP 451 — never circumvented, falls through) → Hyperliquid `metaAndAssetCtxs` (keyless POST; hourly decimal normalized to %/8h) → the v1.2a change-based estimate as fallback.
- `funding_rate_pct` is percent per 8h (Binance convention). Provenance rides along in the market dict: `funding_source` (binance|hyperliquid|estimated|none), `funding_asof`, `funding_age_s`, `funding_estimated`. Stocks/ETFs stay at 0.0 (`none` — no perps exist).
- Jev market context includes funding provenance; the trade-action and squeeze questions tell the model to weigh real venue funding seriously and discount estimates.
- Deterministic squeeze overlay (real funding only): funding ≤ -0.05%/8h floors `squeeze_risk_pct` at 65 (crowded shorts); funding ≥ +0.10%/8h records a `funding_squeeze_note` (crowded longs, long-squeeze risk).
- Env override: `JEV_FUNDING_DISABLE=1` forces the estimated fallback (for testing failure paths).
