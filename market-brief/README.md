# market-brief — twice-daily X sentiment brief

Built on top of the base Jev X sentiment tool in this repo (a fork of
[brainstormity/Jev-X-Sentiment-Analysis](https://github.com/brainstormity/Jev-X-Sentiment-Analysis)).

## What it is

A scheduled market brief: every morning and evening, per asset, pull ~50 recent
X posts and run them through the TypeSafe AI Jev model. Each asset gets a
verdict — BUY / HOLD / TAKE_PROFIT — with confidence, crowd mood, and a
squeeze-risk read.

- Crypto (daily): BTC, ETH, SOL, HYPE, NEAR — market context from Kraken public data
- Stocks/ETFs (weekdays): SMH, DRAM, COIN, HOOD, BE — cashtag-only X queries,
  caller-supplied price since Kraken has no stock pairs

## Pipeline

```
live X pull (twitterapi.io) → local dedup tweet store → Jev model verdict → append-only CSV log
```

- The local tweet store is newest-first, deduped by tweet ID, capped at 2000 per
  symbol — thin cashtags fill the sample from history.
- Pagination halts on the first already-stored tweet ID; the rest of the sample
  fills from the local store (saves API credits).
- No mock data anywhere: the CLI refuses to run on fake inputs.

## Usage

```bash
python3 market-brief/jev_analyze.py BTC --tweets 50
python3 market-brief/jev_analyze.py COIN --tweets 50 --price 204.92 --change-pct 5.49
```

Credentials (twitterapi.io, TypeSafe AI) load from the environment / secure
store — never hardcoded.

## v1.1 roadmap

- **Author-prior layer**: per-author rolling baseline per ticker; today's post
  scored as a delta from the author's own baseline (sarcasm becomes an outlier
  problem, not a text problem). Quote-tweets scored separately from plain posts.
- **Falsifier per call**: every sentiment call carries "this would change my
  mind: ___", plus the quote and link, dated.
- **Daily hit-rate rows**: predicted direction vs actual move, one line per asset
  per day — after two weeks the misses become the labeled training set for
  round two.

Ideas shaped in the open with Turbo, Mikey, muchi, and Z on Musebook.
