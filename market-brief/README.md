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

## v1.1 (on the `dev` branch)

Everything above still holds. v1.1 adds the layers designed in the open with
Turbo, Mikey, muchi, and Z — applied identically to crypto and stocks/ETFs.
Stock/ETF prices keep coming from free public quote data via `--price`; no
paid feed anywhere in the pipeline.

- **Author-prior layer** (`data/author_priors.json`): per-author rolling
  baseline over their last 10 posts per ticker. Each post is scored as a
  *delta* from the author's own baseline — sarcasm becomes an outlier
  problem, not a text problem. |delta| >= 0.6 raises `sarcasm_flag`, and the
  Jev prompt is told to weight the baseline over the literal text.
- **Quote-tweets scored separately**: detected from the payload; a
  quote-tweet with ~empty commentary is an endorsement — scored from the
  author's track record, not the words.
- **Per-call template**: every representative tweet ships with its X link,
  timestamp, author, polarity, baseline, delta, and sarcasm flag.
- **Falsifier per verdict**: "This would change my mind: ___", generated
  from the actual verdict and trade levels (template-grounded in v1.1;
  model-written free text is the v1.2 upgrade).
- **Hit-rate log** (`data/hit_rate.jsonl`): each run appends a
  predicted-direction row; `--settle` fills in actuals ~24h later (crypto via
  Kraken, stocks via `--settle-prices` from the same public quote data) and
  scores hits/misses. Misses are the labeled dataset for round two.

New flags: `--settle`, `--settle-prices "COIN=205.1,HOOD=127.3"`.
Outside the Hatch runtime, credentials fall back to `TWITTERAPI_IO_KEY` and
`TYPESAFE_API_KEY` env vars.
