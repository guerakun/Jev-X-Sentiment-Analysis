#!/usr/bin/env python3
"""Jev X sentiment analysis for one symbol (crypto or stock/ETF).

Pipeline (mirrors brainstormity/Jev-X-Sentiment-Analysis):
  1. Market data: direct Kraken public REST for crypto (urllib, no key):
     price, 24h change, RSI-14, volume, estimated funding rate. For stocks/
     ETFs pass --price and --change-pct instead (Kraken has no stock pairs);
     RSI/funding then default to neutral.
  2. Tweets via twitterapi.io advanced_search (surrogate X-API-Key), with
     early-stopping dedup against locally stored tweet bodies to save credits.
     Crypto uses cashtag OR full name; stocks/ETFs use cashtag only.
  3. Deterministic social stats (fear/greed polarity, author diversity,
     stratified sample of top-engaged + latest tweets).
  4. TypeSafe Jev System One evaluation (surrogate Bearer auth) -> trade
     action, sentiment spectrum, short-squeeze risk, catalyst impact.

Refuses to run on mock/simulated data: every external call must succeed with
real credentials, otherwise it exits non-zero.

Usage:
  jev_analyze.py BTC [--tweets 50]
  jev_analyze.py COIN --tweets 50 --price 312.45 --change-pct 1.2

Output: JSON decision object on stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_IDS_PATH = os.path.join(SKILL_DIR, "data", "seen_ids.json")
TWEET_STORE_PATH = os.path.join(SKILL_DIR, "data", "tweets_store.json")
MAX_STORED_TWEETS_PER_SYMBOL = 2000

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


def die(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(1)


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
        dc.add_surrogate_to_request(
            req, "custom.twitterapi-io",
            entry_name="access_token", allowed_hosts=TWITTER_HOSTS,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return dc.read_json_response(resp)
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




KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "SOL": "SOLUSD",
    "HYPE": "HYPEUSD", "NEAR": "NEARUSD",
}


def kraken_get(path: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://api.kraken.com/0/public/{path}?{qs}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    sample: list[dict] = []
    for t in by_eng[:25]:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            sample.append({"author": t.get("author_username"), "text": t.get("text"),
                           "likes": t.get("likes"), "type": "high_engagement"})
    for t in tweets[:25]:
        if t["id"] not in seen_ids:
            seen_ids.add(t["id"])
            sample.append({"author": t.get("author_username"), "text": t.get("text"),
                           "likes": t.get("likes"), "type": "latest_breaking"})

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


def typesafe_evaluate(symbol: str, market: dict, stats: dict) -> dict:
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
        "representative_tweets": stats["stratified_sample"],
    }
    questions = {
        "trade_action": {
            "type": "choice",
            "instructions": (
                "Given `market` data (RSI, price change, funding rate) and `social_stats` "
                "across `sample_size` tweets, what is the best immediate trading action for `asset`?"
            ),
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
            "instructions": "Rate the prevailing social mood in `social_stats` and `representative_tweets`.",
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
    dc.add_surrogate_to_request(
        req, "custom.typesafe-ai",
        entry_name="access_token", allowed_hosts=TYPESAFE_HOSTS,
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = dc.read_json_response(resp)
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


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="e.g. BTC or COIN")
    ap.add_argument("--tweets", type=int, default=50, help="target tweet sample size")
    ap.add_argument("--price", type=float, default=None,
                    help="current price; required for non-Kraken symbols (stocks/ETFs) "
                         "since Kraken has no stock pairs — skips the Kraken fetch")
    ap.add_argument("--change-pct", type=float, default=0.0,
                    help="24h percent change to pair with --price")
    ap.add_argument("--no-early-stop", action="store_true",
                    help="disable first-known-ID early stopping: paginate past stored "
                         "tweets until --tweets fresh tweets are collected (used to "
                         "bootstrap the historical store; costs more API pages)")
    args = ap.parse_args()

    sym = args.symbol.upper().replace("$", "")
    if args.price is not None:
        # Stock/ETF path: Kraken has no stock pairs, so the caller supplies the
        # price from the regular price lookup. RSI/funding default to neutral.
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
    decision = typesafe_evaluate(sym, market, stats)
    decision["trade_levels"] = build_levels(decision["action"], market["price"])

    out = {
        "ok": True,
        "symbol": sym,
        "analyzed_at": int(time.time()),
        "market": market,
        "social": {k: v for k, v in stats.items() if k != "stratified_sample"},
        "tweets_newly_fetched": tw["newly_fetched"],
        "tweets_supplemented_from_store": tw.get("supplemented_from_store", 0),
        "twitter_api_pages": tw["api_pages_called"],
        "twitter_early_stopped": tw["early_stopped"],
        "no_early_stop_mode": args.no_early_stop,
        "decision": decision,
        "is_mock": False,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
