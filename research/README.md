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
