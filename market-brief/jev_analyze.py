#!/usr/bin/env python3
"""Jev X sentiment analysis for one symbol (crypto or stock/ETF) — v1.1 dev.

Applies to BOTH crypto and stocks/ETFs: crypto market data comes from Kraken
public REST; stock/ETF prices come from free public quote data passed via
--price (no paid feed needed). Author priors, quote-tweet handling, per-call
templates, falsifiers, and hit-rate rows all work identically for both.

v1.1 adds, per the Musebook design thread (Turbo, Mikey, muchi, Z):
  1. Author-prior layer: per-author rolling baseline over their last 10 posts
     per ticker (data/author_priors.json). Each post is scored as a DELTA from
     the author's own baseline — sarcasm becomes an outlier problem, not a
     text problem. Large |delta| raises sarcasm_flag.
  2. Quote-tweet treatment: quote-tweets are detected and scored separately.
     A quote-tweet with ~empty commentary is an endorsement: it carries no new
     information and is scored from the author's track record (baseline), not
     the words.
  3. Per-call template: every representative tweet ships with its X link,
     timestamp, author, quote flag, polarity, author baseline, delta, and
     sarcasm flag.
  4. Falsifier per verdict: "This would change my mind: ___", generated from
     the actual verdict and trade levels (template-grounded for v1.1;
     model-written free text is the v1.2 upgrade).
  5. Hit-rate log: every run appends a predicted-direction row to
     data/hit_rate.jsonl; --settle fills in actuals ~24h later and scores
     hits/misses. Misses are the labeled dataset for round two.

Pipeline (unchanged from v1.0):
  1. Market data: direct Kraken public REST for crypto (urllib, no key):
     price, 24h change, RSI-14, volume, estimated funding rate. For stocks/
     ETFs pass --price and --change-pct instead (Kraken has no stock pairs);
     RSI/funding then default to neutral.
  2. Tweets via twitterapi.io advanced_search, with early-stopping dedup
     against locally stored tweet bodies to save credits. Crypto uses cashtag
     OR full name; stocks/ETFs use cashtag only.
  3. Deterministic social stats (fear/greed polarity, author diversity,
     stratified sample of top-engaged + latest tweets).
  4. TypeSafe Jev System One evaluation -> trade action, sentiment spectrum,
     short-squeeze risk, catalyst impact. Representative tweets now carry
     author baselines and deltas so the model reads sarcasm as outlier.

Refuses to run on mock/simulated data: every external call must succeed with
real credentials, otherwise it exits non-zero.

Usage:
  jev_analyze.py BTC [--tweets 50]
  jev_analyze.py COIN --tweets 50 --price 312.45 --change-pct 1.2
  jev_analyze.py --settle [--settle-prices COIN=205.1,HOOD=127.3]

Output: JSON decision object on stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
    import dynamic_credentials as dc
except Exception:
    dc = None  # outside the Hatch runtime: fall back to env-var credentials

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# v1.1: data lives next to the script so the repo copy is self-contained.
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
SEEN_IDS_PATH = os.path.join(DATA_DIR, "seen_ids.json")
TWEET_STORE_PATH = os.path.join(DATA_DIR, "tweets_store.json")
AUTHOR_PRIORS_PATH = os.path.join(DATA_DIR, "author_priors.json")
HIT_RATE_PATH = os.path.join(DATA_DIR, "hit_rate.jsonl")
MAX_STORED_TWEETS_PER_SYMBOL = 2000
PRIOR_WINDOW = 10          # author's last N posts per ticker (Mikey's number)
SARCASM_DELTA_THRESHOLD = 0.6  # |delta| at or above this => breaking character
FLAT_DEADBAND_PCT = 0.5    # |24h change| under this counts as "flat"
SETTLE_MIN_AGE_H = 20      # settle rows at least this old

TWITTER_API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
TWITTER_HOSTS = ("api.twitterapi.io",)
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_HOSTS = ("api.typesafe.ai",)

SYMBOL_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "HYPE": "Hyperliquid",
    "NEAR": "Near", "DOGE": "Dogecoin", "XRP": "Ripple", "ADA": "Cardano",
    "AVAX": "Avalanche", "SUI": "Sui",
}

FEAR_KEYWORDS = {
    "crash", "dump", "liquidation", "sell", "selling", "dead", "bear", "bearish",
    "scam", "rekt", "drop", "loss", "bleeding", "panic", "fear", "down", "dip", "fall",
}
GREED_KEYWORDS = {
    "pump", "moon", "ath", "buy", "buying", "gem", "bull", "bullish", "breakout",
    "rally", "gain", "accumulate", "rocket", "up", "long", "hold", "squeeze",
}

DIRECTION_OF = {
    "STRONG_BUY": "up", "BUY": "up",
    "SELL": "down", "STRONG_SELL": "down",
    "HOLD": "flat", "TAKE_PROFIT": "down",
}


def die(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(1)


def read_json(resp) -> dict:
    if dc is not None:
        return dc.read_json_response(resp)
    return json.loads(resp.read().decode("utf-8"))


def auth_request(req, provider: str, entry_name: str, allowed_hosts,
                 env_name: str, header_kind: str) -> None:
    """Attach auth: Hatch surrogate credential first, env var as fallback."""
    if dc is not None:
        try:
            dc.add_surrogate_to_request(
                req, provider, entry_name=entry_name, allowed_hosts=allowed_hosts)
            return
        except Exception:
            pass
    key = os.environ.get(env_name)
    if not key:
        die(f"no credential for {provider}: secure store unavailable and "
            f"{env_name} not set (refusing mock)")
    if header_kind == "x-api-key":
        req.add_header("X-API-Key", key)
    else:
        req.add_header("Authorization", f"Bearer {key}")


def load_seen_ids() -> dict:
    try:
        with open(SEEN_IDS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_seen_ids(data: dict) -> None:
    os.makedirs(os.path.dirname(SEEN_IDS_PATH), exist_ok=True)
    tmp = SEEN_IDS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SEEN_IDS_PATH)


def load_tweet_store() -> dict:
    try:
        with open(TWEET_STORE_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tweet_store(store: dict) -> None:
    os.makedirs(os.path.dirname(TWEET_STORE_PATH), exist_ok=True)
    tmp = TWEET_STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f)
    os.replace(tmp, TWEET_STORE_PATH)


# ---------------------------------------------------------------- v1.1: author priors

def load_author_priors() -> dict:
    """{SYMBOL: {author_lower: [{ts, polarity, is_quote}, ...last 10...]}}"""
    try:
        with open(AUTHOR_PRIORS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_author_priors(priors: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = AUTHOR_PRIORS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(priors, f)
    os.replace(tmp, AUTHOR_PRIORS_PATH)


def compute_polarity(text: str) -> float:
    """Keyword polarity in [-1, 1]; the per-post score the delta is measured on."""
    words = set(text.lower().replace("$", "").replace("#", "").split())
    f = len(words & FEAR_KEYWORDS)
    g = len(words & GREED_KEYWORDS)
    tot = f + g
    return round((g - f) / tot, 3) if tot else 0.0


def author_baseline(priors: dict, sym: str, author: str):
    """Mean polarity of the author's last PRIOR_WINDOW posts on this ticker."""
    hist = ((priors.get(sym) or {}).get((author or "anonymous").lower()) or [])
    if not hist:
        return None, 0
    vals = [h.get("polarity", 0.0) for h in hist]
    return round(sum(vals) / len(vals), 3), len(hist)


def update_author_priors(priors: dict, sym: str, tweets: list[dict]) -> None:
    """Fold this run's tweets into the rolling per-author baselines."""
    sp = priors.setdefault(sym, {})
    for t in tweets:
        a = (t.get("author_username") or "anonymous").lower()
        hist = sp.setdefault(a, [])
        hist.append({
            "ts": t.get("created_at"),
            "polarity": compute_polarity(t.get("text", "")),
            "is_quote": bool(t.get("is_quote_tweet")),
        })
        del hist[:-PRIOR_WINDOW]
    save_author_priors(priors)


def is_quote_tweet(raw: dict) -> bool:
    """Defensive quote-tweet detection across twitterapi.io field variants."""
    return bool(
        raw.get("quotedTweet") or raw.get("quoted_tweet")
        or raw.get("quoteTweet") or raw.get("quoted_status")
        or raw.get("isQuote") or raw.get("is_quote")
    )


def comment_length(text: str) -> int:
    """Length of the tweet with URLs stripped — the actual commentary."""
    return len(re.sub(r"https?://\S+", "", text or "").strip())


# ---------------------------------------------------------------- tweets

def build_query(symbol: str) -> str:
    sym = symbol.upper().replace("$", "")
    full = SYMBOL_NAMES.get(sym, sym)
    if sym == full:
        return f"${sym} lang:en -is:retweet min_faves:2"
    return f"(${sym} OR {full}) lang:en -is:retweet min_faves:2"


def twitter_get(params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(f"{TWITTER_API_URL}?{qs}", method="GET")
        auth_request(req, "custom.twitterapi-io", "access_token", TWITTER_HOSTS,
                     "TWITTERAPI_IO_KEY", "x-api-key")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return read_json(resp)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 5)
                continue
            raise
    raise last_err


def fetch_tweets(symbol: str, target_count: int, no_early_stop: bool = False) -> dict:
    """Fetch tweets with early-stopping dedup + historical store supplementation.

    Mirrors brainstormity/Jev-X-Sentiment-Analysis: new tweets come from the
    API (stopping at the first already-known ID to save credits), then the
    sample is topped up to target_count from the local historical tweet store
    so a run never collapses to zero tweets just because dedup hit early.

    IMPORTANT: "already known" is derived ONLY from tweet IDs that have full
    bodies stored in tweets_store.json. The legacy seen_ids.json cache may
    hold IDs with no stored bodies; it is kept as passive tracking only and
    must never trigger early stopping or suppress fresh fetches on its own.

    no_early_stop=True disables the first-known-ID stop (used when building
    the historical store): known IDs are skipped and pagination continues
    until target_count fresh tweets are collected.
    """
    sym = symbol.upper().replace("$", "")
    seen = load_seen_ids()
    store = load_tweet_store()
    stored = store.get(sym, [])
    # Known = IDs with stored bodies only. Legacy seen_ids never gates fetch.
    known_ids = {str(t.get("id", "")) for t in stored if t.get("id")}

    new_tweets: list[dict] = []
    cursor = None
    pages = 0
    max_pages = max(1, (target_count + 39) // 40)
    early_stopped = False
    query = build_query(sym)
    fallback_query = f"${sym} lang:en -is:retweet"  # looser, no min_faves
    used_fallback = False

    def fetch_page(q: str, cur) -> dict:
        """One paged request; retries when the API returns an empty page,
        since twitterapi.io sometimes returns zero results transiently."""
        params = {"query": q, "queryType": "Latest"}
        if cur:
            params["cursor"] = cur
        last_err = None
        for retry in range(3):
            try:
                data = twitter_get(params)
            except Exception as e:
                last_err = e
                time.sleep(5 * (retry + 1))
                continue
            raw = data.get("tweets") or data.get("data") or []
            if raw or retry == 2:
                return data
            time.sleep(5 * (retry + 1))
        if last_err is not None:
            die(f"twitterapi.io request failed: {last_err}")
        return data

    while len(new_tweets) < target_count and pages < max_pages:
        pages += 1
        before = len(new_tweets)
        data = fetch_page(query, cursor)
        raw = data.get("tweets") or data.get("data") or []
        if not raw:
            # First-page total miss: try the looser fallback query once before
            # giving up (still real data only, never mocked).
            if pages == 1 and not used_fallback:
                used_fallback = True
                data = fetch_page(fallback_query, None)
                raw = data.get("tweets") or data.get("data") or []
            if not raw:
                break
        for t in raw:
            tid = str(t.get("id", ""))
            if not tid:
                continue
            if tid in known_ids:
                if no_early_stop:
                    continue  # skip known, keep paginating for fresh tweets
                early_stopped = True
                break
            author = t.get("author") or {}
            new_tweets.append({
                "id": tid,
                "text": t.get("text", ""),
                "created_at": t.get("createdAt") or t.get("created_at", ""),
                "likes": int(t.get("likeCount") or t.get("likes") or 0),
                "retweets": int(t.get("retweetCount") or t.get("retweets") or 0),
                "replies": int(t.get("replyCount") or t.get("replies") or 0),
                "author_username": author.get("userName") or author.get("username") or "anonymous",
                "author_followers": int(author.get("followers") or author.get("followersCount") or 0),
                "author_verified": bool(author.get("isBlueVerified") or author.get("verified") or False),
                "is_quote_tweet": is_quote_tweet(t),  # v1.1: Turbo/Z quote-tweet split
            })
        if early_stopped:
            break
        if len(new_tweets) == before and no_early_stop:
            # Pagination is returning only already-stored tweets; further
            # pages would burn credits for nothing.
            break
        cursor = data.get("next_cursor") or data.get("cursor")
        if not cursor:
            break

    # Persist new tweets to the historical store (newest first, deduped, capped).
    if new_tweets:
        merged = {t["id"]: t for t in new_tweets}
        for t in stored:
            merged.setdefault(str(t.get("id", "")), t)
        store[sym] = list(merged.values())[:MAX_STORED_TWEETS_PER_SYMBOL]
        save_tweet_store(store)
        known_ids.update(t["id"] for t in new_tweets)
        # seen_ids.json stays as passive bookkeeping only: merged legacy IDs
        # are retained for audit, but this file never gates fetching (see the
        # known_ids construction above).
        legacy = set(seen.get(sym, []))
        seen[sym] = sorted(known_ids | legacy)[-5000:]
        save_seen_ids(seen)
        stored = store.get(sym, [])

    # Supplement up to target_count from the historical store so the sample
    # never collapses when early stopping fires on the first page.
    combined = list(new_tweets)
    if len(combined) < target_count:
        have = {t["id"] for t in combined}
        for t in stored:
            tid = str(t.get("id", ""))
            if tid not in have:
                combined.append(t)
                have.add(tid)
                if len(combined) >= target_count:
                    break

    return {
        "symbol": sym,
        "tweets": combined,
        "newly_fetched": len(new_tweets),
        "api_pages_called": pages,
        "early_stopped": early_stopped,
        "supplemented_from_store": len(combined) - len(new_tweets),
        "is_mock": False,
    }

# ---------------------------------------------------------------- market data

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD",
    "HYPE": "HYPEUSD", "NEAR": "NEARUSD",
}


def kraken_get(path: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://api.kraken.com/0/public/{path}?{qs}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def kraken_price(sym: str):
    """Current price for a crypto symbol, or None (never dies; for --settle)."""
    pair = KRAKEN_PAIRS.get(sym)
    if not pair:
        return None
    try:
        tdata = kraken_get("Ticker", {"pair": pair})
        if tdata.get("error"):
            return None
        tkey = next(iter(tdata["result"]))
        return float(tdata["result"][tkey]["c"][0])
    except Exception:
        return None


def fetch_market(symbol: str) -> dict:
    """Market data via Kraken public REST (no key). urllib honors the egress proxy."""
    sym = symbol.upper().replace("$", "")
    pair = KRAKEN_PAIRS.get(sym)
    if not pair:
        die(f"no Kraken pair mapping for {sym}")
    try:
        tdata = kraken_get("Ticker", {"pair": pair})
        if tdata.get("error"):
            raise RuntimeError("; ".join(tdata["error"]))
        tkey = next(iter(tdata["result"]))
        t = tdata["result"][tkey]
        price = float(t["c"][0])
        open_24h = float(t["o"])
        high_24h = float(t["h"][0])
        low_24h = float(t["l"][0])
        volume_24h = float(t["v"][1]) * price
        change_24h = (price - open_24h) / open_24h * 100.0

        odata = kraken_get("OHLC", {"pair": pair, "interval": 60})
        if odata.get("error"):
            raise RuntimeError("; ".join(odata["error"]))
        okey = next(iter(odata["result"]))
        closes = [float(c[4]) for c in odata["result"][okey][-48:]]
        rsi = calculate_rsi(closes, 14)

        funding = 0.010 if change_24h > 3 else (-0.015 if change_24h < -3 else 0.005)
        return {
            "symbol": sym,
            "price": round(price, 4) if price < 10 else round(price, 2),
            "change_24h_pct": round(change_24h, 2),
            "high_24h": round(high_24h, 2),
            "low_24h": round(low_24h, 2),
            "volume_24h_usd": round(volume_24h, 0),
            "rsi_14": rsi,
            "funding_rate_pct": round(funding, 4),
            "is_fallback": False,
            "source": "kraken",
        }
    except SystemExit:
        raise
    except Exception as e:
        die(f"market data fetch failed for {sym}: {e}")


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        ch = prices[i] - prices[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def process_tweets(tweets: list[dict]) -> dict:
    if not tweets:
        die("no tweets available for sentiment scoring (refusing mock data)")
    total_likes = total_retweets = 0
    authors: set[str] = set()
    fear_count = greed_count = 0
    for t in tweets:
        total_likes += t.get("likes", 0)
        total_retweets += t.get("retweets", 0)
        authors.add((t.get("author_username") or "unknown").lower())
        words = set(t.get("text", "").lower().replace("$", "").replace("#", "").split())
        fear_count += len(words & FEAR_KEYWORDS)
        greed_count += len(words & GREED_KEYWORDS)

    n = len(tweets)
    total_polar = fear_count + greed_count
    polarity = round((greed_count - fear_count) / total_polar, 2) if total_polar else 0.0
    if polarity <= -0.4:
        label = "Extreme Panic"
    elif polarity < -0.1:
        label = "Bearish / Fearful"
    elif polarity <= 0.1:
        label = "Neutral / Mixed"
    elif polarity < 0.4:
        label = "Bullish / Optimistic"
    else:
        label = "Euphoric / Greedy"

    by_eng = sorted(tweets, key=lambda x: x.get("likes", 0) + x.get("retweets", 0) * 2, reverse=True)
    seen_ids: set[str] = set()
    # v1.1: sample entries carry the fields the per-call template needs.
    sample: list[dict] = []
    for t in by_eng[:25]:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            sample.append({"id": t.get("id"), "author": t.get("author_username"),
                           "text": t.get("text"), "created_at": t.get("created_at"),
                           "likes": t.get("likes"), "is_quote_tweet": bool(t.get("is_quote_tweet")),
                           "type": "high_engagement"})
    for t in tweets[:25]:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            sample.append({"id": t.get("id"), "author": t.get("author_username"),
                           "text": t.get("text"), "created_at": t.get("created_at"),
                           "likes": t.get("likes"), "is_quote_tweet": bool(t.get("is_quote_tweet")),
                           "type": "latest_breaking"})

    return {
        "sample_size": n,
        "unique_authors_count": len(authors),
        "author_diversity_pct": round(len(authors) / n * 100.0, 1),
        "total_likes": total_likes,
        "total_retweets": total_retweets,
        "fear_mentions": fear_count,
        "greed_mentions": greed_count,
        "polarity_score": polarity,
        "sentiment_label": label,
        "stratified_sample": sample,
    }


def typesafe_evaluate(symbol: str, market: dict, stats: dict, calls: list[dict]) -> dict:
    # v1.1: representative tweets carry author baselines + deltas so the model
    # reads sarcasm as an outlier, and quote-tweets are marked for separate
    # treatment.
    rep = []
    for c in calls:
        rep.append({
            "author": c.get("author"),
            "text": (c.get("text") or "")[:280],
            "polarity": c.get("polarity"),
            "author_baseline_10": c.get("author_baseline_10"),
            "delta_vs_baseline": c.get("delta_vs_baseline"),
            "sarcasm_flag": c.get("sarcasm_flag"),
            "is_quote_tweet": c.get("is_quote_tweet"),
            "endorsement_by_quote": c.get("endorsement_by_quote"),
        })
    author_guidance = (
        "Each representative tweet carries author_baseline_10 (that author's mean tone "
        "over their last 10 posts on this ticker) and delta_vs_baseline. When sarcasm_flag "
        "is true the author is breaking character: read the post as possible sarcasm/irony "
        "and weight their baseline over the literal text. Tweets flagged endorsement_by_quote "
        "are quote-tweets with ~empty commentary: they carry no new information, only "
        "endorsement — score them from the author's track record, not the words."
    )
    state = {
        "asset": symbol,
        "market": {
            "current_price": market["price"],
            "change_24h_pct": market["change_24h_pct"],
            "rsi_14": market["rsi_14"],
            "funding_rate_pct": market["funding_rate_pct"],
            "volume_24h_usd": market["volume_24h_usd"],
        },
        "social_stats": {
            "sample_size": stats["sample_size"],
            "author_diversity_pct": stats["author_diversity_pct"],
            "total_likes": stats["total_likes"],
            "polarity_score": stats["polarity_score"],
            "sentiment_label": stats["sentiment_label"],
        },
        "representative_tweets": rep,
    }
    questions = {
        "trade_action": {
            "type": "choice",
            "instructions": (
                "Given `market` data (RSI, price change, funding rate) and `social_stats` "
                "across `sample_size` tweets, what is the best immediate trading action for `asset`? "
            ) + author_guidance,
            "criteria": {
                "STRONG_BUY": "High-conviction long (e.g. short squeeze setup, capitulation bottom, or major verified breakout).",
                "BUY": "Favorable risk-to-reward long entry with positive upside expectation.",
                "HOLD": "Neutral, range-bound, or consolidating; no clear asymmetric statistical edge.",
                "TAKE_PROFIT": "Market is overbought or meeting heavy resistance; secure existing gains.",
                "SELL": "Bearish breakdown, deteriorating momentum, or high downside continuation risk.",
                "STRONG_SELL": "Crowded top exhaustion, extreme positive funding, or severe fundamental catalyst breakdown.",
            },
        },
        "sentiment_spectrum": {
            "type": "score",
            "instructions": "Rate the prevailing social mood in `social_stats` and `representative_tweets`. " + author_guidance,
            "criteria": [
                "Extreme Panic / Capitulation",
                "Cautious / Bearish",
                "Neutral / Mixed",
                "Optimistic / Bullish",
                "Euphoric / Greedy",
            ],
        },
        "is_short_squeeze_risk": {
            "type": "noul",
            "instructions": (
                "Does the state show negative `market.funding_rate_pct` clashing with "
                "`social_stats.sentiment_label` panic at support, indicating a short squeeze risk?"
            ),
        },
        "catalyst_impact": {
            "type": "score",
            "instructions": "Rate the significance of any events or breaking news described in `representative_tweets`.",
            "criteria": [
                "No news or pure retail noise",
                "Minor routine update or rumors",
                "Moderate ecosystem milestone",
                "Major market-shifting catalyst",
            ],
        },
    }
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    req = urllib.request.Request(TYPESAFE_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    auth_request(req, "custom.typesafe-ai", "access_token", TYPESAFE_HOSTS,
                 "TYPESAFE_API_KEY", "bearer")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = read_json(resp)
    except Exception as e:
        die(f"TypeSafe Jev evaluation failed: {e} (refusing mock fallback)")

    answers = payload.get("answers") or {}
    action_ans = answers.get("trade_action") or {}
    sent_ans = answers.get("sentiment_spectrum") or {}
    squeeze_ans = answers.get("is_short_squeeze_risk") or {}
    catalyst_ans = answers.get("catalyst_impact") or {}

    trade_action = action_ans.get("choice", "HOLD")
    confidence = round(float(action_ans.get("confidence", 0.75)) * 100, 1)
    raw_probs = action_ans.get("probabilities") or {}
    probs = {}
    for k, v in raw_probs.items():
        val = float(v)
        if 0.0 < val <= 1.0:
            val *= 100.0
        probs[k] = round(val, 1)

    sentiment_levels = [
        "Extreme Panic / Capitulation", "Cautious / Bearish", "Neutral / Mixed",
        "Optimistic / Bullish", "Euphoric / Greedy",
    ]
    s_score = float(sent_ans.get("score", 2.0))
    s_idx = min(max(0, int(round(s_score))), len(sentiment_levels) - 1)

    return {
        "action": trade_action,
        "confidence_pct": confidence,
        "action_probabilities": probs,
        "sentiment_label": sentiment_levels[s_idx],
        "sentiment_score": round(s_score, 2),
        "squeeze_risk_pct": round(float(squeeze_ans.get("noul", 0.15)) * 100, 1),
        "catalyst_impact_score": round(float(catalyst_ans.get("score", 0.0)), 2),
        "is_mock": False,
    }


def build_levels(action: str, price: float) -> dict:
    if action in ("STRONG_BUY", "BUY"):
        entry = [round(price * 0.995, 2), round(price * 1.002, 2)]
        sl, tp1, tp2 = price * 0.962, price * 1.045, price * 1.085
    elif action in ("SELL", "STRONG_SELL"):
        entry = [round(price * 0.998, 2), round(price * 1.005, 2)]
        sl, tp1, tp2 = price * 1.038, price * 0.955, price * 0.915
    elif action == "TAKE_PROFIT":
        entry = [round(price * 0.99, 2), round(price * 1.01, 2)]
        sl, tp1, tp2 = price * 0.98, price * 1.02, price * 1.05
    else:
        entry = [price, price]
        sl, tp1, tp2 = price * 0.95, price * 1.05, price * 1.10
    sl, tp1, tp2 = round(sl, 2), round(tp1, 2), round(tp2, 2)
    sl_pct = round((sl - price) / price * 100, 2)
    tp2_pct = round((tp2 - price) / price * 100, 2)
    return {
        "entry_range": entry, "stop_loss": sl, "stop_loss_pct": sl_pct,
        "target_1": tp1, "target_1_pct": round((tp1 - price) / price * 100, 2),
        "target_2": tp2, "target_2_pct": tp2_pct,
        "risk_reward_ratio": round(abs(tp2_pct / sl_pct), 2) if sl_pct else 2.5,
    }

# ---------------------------------------------------------------- v1.1: per-call template, falsifier, hit-rate

def build_calls(sample: list[dict], priors: dict, sym: str) -> list[dict]:
    """Per-call template: link, timestamp, author, quote flag, polarity,
    author baseline, delta, sarcasm flag. Baselines are computed BEFORE this
    run's tweets are folded into the priors, so the delta is honest."""
    calls = []
    for s in sample:
        author = s.get("author") or "anonymous"
        polarity = compute_polarity(s.get("text", ""))
        baseline, n = author_baseline(priors, sym, author)
        is_quote = bool(s.get("is_quote_tweet"))
        # Z's rule: quote-tweet with ~empty commentary = endorsement, scored
        # from the author's track record, not the words.
        endorsement = is_quote and comment_length(s.get("text", "")) < 15
        eff_polarity = baseline if (endorsement and baseline is not None) else polarity
        delta = round(eff_polarity - baseline, 3) if baseline is not None else 0.0
        sarcasm = (
            baseline is not None
            and abs(delta) >= SARCASM_DELTA_THRESHOLD
            and not endorsement
        )
        tid = s.get("id")
        calls.append({
            "tweet_id": tid,
            "url": f"https://x.com/i/status/{tid}" if tid else None,
            "created_at": s.get("created_at"),
            "author": author,
            "sample_type": s.get("type"),
            "text": (s.get("text") or "")[:280],
            "is_quote_tweet": is_quote,
            "endorsement_by_quote": endorsement,
            "polarity": eff_polarity,
            "author_baseline_10": baseline,
            "baseline_n": n,
            "delta_vs_baseline": delta,
            "sarcasm_flag": sarcasm,
        })
    return calls


def build_falsifier(action: str, sym: str, market: dict, levels: dict) -> str:
    """muchi's falsifier: the kill condition for this verdict, generated from
    the actual verdict and trade levels. (v1.1: template-grounded; v1.2:
    model-written free text.)"""
    lo, hi = levels["entry_range"]
    sl = levels["stop_loss"]
    if action in ("STRONG_BUY", "BUY"):
        return (f"This would change my mind: {sym} breaks below ${sl} on rising "
                f"volume, or the next 50-post sample flips to fear (polarity < -0.3) "
                f"on fresh negative catalysts.")
    if action in ("SELL", "STRONG_SELL"):
        return (f"This would change my mind: {sym} reclaims ${hi} with funding "
                f"flipping positive and the next sample staying greedy (polarity > 0.3) "
                f"without new negative catalysts.")
    if action == "TAKE_PROFIT":
        return (f"This would change my mind: {sym} consolidates above ${hi} for 48h+ "
                f"with RSI cooling under 60 — the overbought read expires and the "
                f"uptrend resumes.")
    return (f"This would change my mind: a decisive break of ${lo}-${hi} in either "
            f"direction on >2x average volume, or a major catalyst in the next sample.")


def append_hit_rate_row(row: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HIT_RATE_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def load_hit_rate_rows() -> list[dict]:
    rows = []
    try:
        with open(HIT_RATE_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except OSError:
        pass
    return rows


def save_hit_rate_rows(rows: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HIT_RATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, HIT_RATE_PATH)


def settle_hit_rate(settle_prices: dict) -> dict:
    """Z's hit-rate rows: fill actuals for rows old enough to judge.

    Crypto settles via Kraken public REST. Stocks/ETFs settle from
    --settle-prices (same free public quote data the main run uses via
    --price); symbols without a settle price are skipped, never guessed.
    Misses are labeled in place: they become the training set for round two.
    """
    rows = load_hit_rate_rows()
    if not rows:
        return {"ok": True, "settled_this_run": 0, "note": "no hit-rate log yet"}
    now = int(time.time())
    settled = hits = 0
    for r in rows:
        if r.get("settled_at"):
            continue
        if now - r.get("analyzed_at", now) < SETTLE_MIN_AGE_H * 3600:
            continue
        sym = r["symbol"]
        price = kraken_price(sym)
        if price is None and settle_prices:
            price = settle_prices.get(sym)
        if price is None:
            continue  # no price source: skip, never mock
        chg = (price - r["price_at_call"]) / r["price_at_call"] * 100.0
        actual = "flat" if abs(chg) < FLAT_DEADBAND_PCT else ("up" if chg > 0 else "down")
        hit = actual == r["predicted_direction"]
        r.update({
            "settled_at": now,
            "settle_price": round(price, 4) if price < 10 else round(price, 2),
            "actual_change_pct": round(chg, 2),
            "actual_direction": actual,
            "hit": hit,
            "is_miss": not hit,
        })
        settled += 1
        hits += 1 if hit else 0
    save_hit_rate_rows(rows)
    total_settled = sum(1 for r in rows if r.get("settled_at"))
    total_hits = sum(1 for r in rows if r.get("hit"))
    return {
        "ok": True,
        "settled_this_run": settled,
        "hits_this_run": hits,
        "all_time_settled": total_settled,
        "all_time_hit_rate_pct": round(total_hits / total_settled * 100, 1) if total_settled else 0.0,
        "misses_labeled": sum(1 for r in rows if r.get("is_miss")),
    }


def parse_settle_prices(s: str) -> dict:
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip().upper().replace("$", "")] = float(v)
            except ValueError:
                pass
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", help="e.g. BTC or COIN")
    ap.add_argument("--tweets", type=int, default=50, help="target tweet sample size")
    ap.add_argument("--price", type=float, default=None,
                    help="current price; required for non-Kraken symbols (stocks/ETFs). "
                         "Pass the free public quote — no paid feed needed.")
    ap.add_argument("--change-pct", type=float, default=0.0,
                    help="24h percent change to pair with --price")
    ap.add_argument("--no-early-stop", action="store_true",
                    help="disable first-known-ID early stopping: paginate past stored "
                         "tweets until --tweets fresh tweets are collected (used to "
                         "bootstrap the historical store; costs more API pages)")
    ap.add_argument("--settle", action="store_true",
                    help="settle old hit-rate rows (fill actuals, score hits/misses) and exit")
    ap.add_argument("--settle-prices", default="",
                    help="comma-separated SYM=price for settling stocks/ETFs, "
                         'e.g. "COIN=205.1,HOOD=127.3" (public quote data)')
    args = ap.parse_args()

    if args.settle:
        print(json.dumps(settle_hit_rate(parse_settle_prices(args.settle_prices)), indent=2))
        return

    if not args.symbol:
        die("symbol is required (or use --settle)")

    sym = args.symbol.upper().replace("$", "")
    if args.price is not None:
        # Stock/ETF path: Kraken has no stock pairs, so the caller supplies the
        # price from the free public quote. RSI/funding default to neutral.
        market = {
            "symbol": sym,
            "price": args.price,
            "change_24h_pct": args.change_pct,
            "high_24h": None,
            "low_24h": None,
            "volume_24h_usd": 0,
            "rsi_14": 50.0,
            "funding_rate_pct": 0.0,
            "is_fallback": False,
            "source": "provided",
        }
    else:
        market = fetch_market(sym)
    tw = fetch_tweets(sym, args.tweets, no_early_stop=args.no_early_stop)
    stats = process_tweets(tw["tweets"])

    # v1.1: baselines come from priors BEFORE this run's tweets are folded in.
    priors = load_author_priors()
    calls = build_calls(stats["stratified_sample"], priors, sym)

    decision = typesafe_evaluate(sym, market, stats, calls)
    decision["trade_levels"] = build_levels(decision["action"], market["price"])

    # v1.1: fold this run into the rolling priors AFTER scoring.
    update_author_priors(priors, sym, tw["tweets"])

    falsifier = build_falsifier(decision["action"], sym, market, decision["trade_levels"])
    hit_row = {
        "date": time.strftime("%Y-%m-%d"),
        "analyzed_at": int(time.time()),
        "symbol": sym,
        "predicted_action": decision["action"],
        "predicted_direction": DIRECTION_OF.get(decision["action"], "flat"),
        "price_at_call": market["price"],
        "confidence_pct": decision["confidence_pct"],
        "falsifier": falsifier,
    }
    append_hit_rate_row(hit_row)

    out = {
        "ok": True,
        "version": "1.1-dev",
        "symbol": sym,
        "analyzed_at": hit_row["analyzed_at"],
        "market": market,
        "social": {k: v for k, v in stats.items() if k != "stratified_sample"},
        "tweets_newly_fetched": tw["newly_fetched"],
        "tweets_supplemented_from_store": tw.get("supplemented_from_store", 0),
        "twitter_api_pages": tw["api_pages_called"],
        "twitter_early_stopped": tw["early_stopped"],
        "no_early_stop_mode": args.no_early_stop,
        "decision": decision,
        "v1_1": {
            "calls": calls,
            "calls_count": len(calls),
            "sarcasm_flags": sum(1 for c in calls if c["sarcasm_flag"]),
            "quote_tweets": sum(1 for c in calls if c["is_quote_tweet"]),
            "endorsement_quotes": sum(1 for c in calls if c["endorsement_by_quote"]),
            "falsifier": falsifier,
            "hit_rate_row": hit_row,
        },
        "is_mock": False,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
