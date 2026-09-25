# research/ — Jev-X Pine research layer

## Licensing boundary (read this before touching anything here)

The Pine runtime used by this directory (LuxAlgo PineTS / pinets-cli) is
**AGPL-3.0**. This directory is the containment zone for that license:

- Everything in `research/` that executes Pine or links the PineTS library is
  AGPL-3.0. If you add files here that import `pinets`, they are AGPL-3.0 too.
- The integration contract with the rest of Jev-X is **a separate OS process**:
  `.pine` file in, JSON on stdout out. `market-brief/jev_analyze.py` (or its
  successor) must shell out to `pinets-cli` and parse stdout — never
  `import pinets` in shipped Python/JS. Arm's-length subprocess + JSON keeps
  the two programs separate works.
- **Do not copy LuxAlgo Library indicator source here.** Their library is
  CC BY-NC-SA 4.0 (non-commercial + share-alike). Reimplement standard
  concepts (BOS/CHoCH, FVG, order blocks, ATR, Supertrend, CMF, RSI) from
  first principles, as `research.pine` does.
- MIT/CC0 LuxAlgo repos (edge-stats, market-trackers, mcp-server) are fine to
  use freely and do not belong in this directory.

## Layout

- `research.pine` — Pine v6 feature script (PoC, validated 2026-09-25 under
  PineTS 0.9.34). Plots only; the runner reads the latest value of each plot.
- Runner contract: bars JSON `[{openTime, open, high, low, close, volume}, ...]`
  oldest-first → stdout JSON `{close, ATR, Supertrend, TrendDir, SwingHigh,
  SwingLow, CMF, BOS_Up, BOS_Down}`. Drop the forming bar **only** when the
  source's last bar is incomplete (Kraken OHLC: yes; Stooq daily: no).

## Rules for new features

1. Max 5 features in the Jev decision state. A feature earns its place via the
   hit-rate log, not backtest aesthetics.
2. Closed bars only. No repainting inputs to the decision.
3. Features are **context for the Jev model**, never standalone signals.

## Validation log

- 2026-09-25: `pinets-cli@0.1.15` ran `research.pine` over 721 real Kraken
  XBTUSD 4h bars via `--data bars.json`. All 8 plots returned values:
  ATR=971.37, Supertrend=86903.79, TrendDir=1, SwingHigh=87274.40,
  SwingLow=82832.30, CMF=-0.0757, BOS_Up=0, BOS_Down=0.
- TrendDir sign convention confirmed empirically: price below the Supertrend
  line returned dir=1 (downtrend); dir<0 = uptrend, matching the comment in
  `research.pine`.
- Note: when extracting features, skip the last bar of Kraken OHLC (forming
  interval) — or better, drop it before invoking the CLI (timeframe-aware).
- 2026-09-25: stock path validated — Yahoo Finance chart API (keyless)
  returned 251 daily COIN bars; `research.pine` computed ATR=11.75,
  Supertrend=164.59, TrendDir=-1 (uptrend, consistent with the verified sign
  convention), CMF≈0. Stooq's CSV endpoint timed out from this environment,
  so Yahoo is the stock-bars source (docs updated accordingly).
- TODO for v1.2a: `research.pine` still needs an `RSI(14)` plot for the stock
  path (gap #2: stocks currently get RSI=50.0 neutral).

## Implementation status (2026-09-25, v1.2a)

`market-brief/jev_analyze.py` now wires this layer in:

- **Crypto:** Kraken 4h OHLC → `run_research()` (pinets-cli subprocess) → `compact_research()` → `market["research"]`. The forming 4h candle is dropped before the run.
- **Stocks/ETFs:** Yahoo Finance daily chart bars (keyless) → same pipeline; the daily RSI-14 replaces the old hardcoded `50.0`. Only today's still-forming session bar is dropped.
- **Jev state:** `market.research` carries at most 5 features (`timeframe, atr_pct, trend, cmf_20, rsi, bos`) as *context*, with prompt wording that they are not standalone signals.
- **`build_levels()`:** volatility-adaptive when research is present — stop = 1.5x ATR, T1 = 1.5R, T2 = swing structure (validated: beyond T1 in the trade direction) else 3R. `levels_basis` tags `atr_1.5x_4h` / `atr_1.5x_1d` vs `fixed_v1.1`.
- Degrades gracefully: if pinets-cli is missing or a run fails, research is `None` and the pipeline falls back to v1.1 fixed levels / neutral RSI — the core sentiment read never dies on research-layer trouble.
- Env overrides: `JEV_PINETS_CLI`, `JEV_RESEARCH_PINE`.

Funding rates remain estimated in v1.2a (v1.2b workstream).
