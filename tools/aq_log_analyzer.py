#!/usr/bin/env python3
"""
Analyze AQ logs for adverse selection and markouts.

Parses:
- Quote decision lines suggest mid/bid/ask for token prefixes.
- Fill lines from user_channel_manager.

Outputs a summary and optional CSV of per-fill metrics.
"""
from __future__ import annotations

import argparse
import csv
import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S,%f"

QUOTE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
    r"Quote decision for (?P<token>[0-9a-fA-F]+)\.\.\.: PLACE_QUOTE - .*?"
    r"best_bid=(?P<best_bid>[0-9.]+)\s+"
    r"best_ask=(?P<best_ask>[0-9.]+)\s+"
    r"mid=(?P<mid>[0-9.]+|N/A)\s+"
    r"bid=(?P<bid>[0-9.]+)\s+ask=(?P<ask>[0-9.]+)"
)

FILL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
    r"Fill: trade=.*? token=(?P<token>[0-9a-fA-F]+)\.\.\. "
    r"side=(?P<side>BUY|SELL) price=(?P<price>[0-9.]+) size=(?P<size>[0-9.]+)"
)


@dataclass
class QuoteObs:
    ts: datetime
    mid: float
    best_bid: float
    best_ask: float


@dataclass
class FillObs:
    ts: datetime
    token: str
    side: str
    price: float
    size: float


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TIMESTAMP_FMT)


def _edge_bps(side: str, price: float, mid: float) -> Optional[float]:
    if mid <= 0:
        return None
    if side == "BUY":
        return (mid - price) / mid * 10000
    return (price - mid) / mid * 10000


def _markout_bps(side: str, price: float, future_mid: float) -> Optional[float]:
    if price <= 0:
        return None
    if side == "BUY":
        return (future_mid - price) / price * 10000
    return (price - future_mid) / price * 10000


def _find_quote_before(quotes: List[QuoteObs], ts: datetime) -> Optional[QuoteObs]:
    if not quotes:
        return None
    times = [q.ts for q in quotes]
    idx = bisect_left(times, ts)
    if idx == 0:
        return None
    return quotes[idx - 1]


def _find_mid_after(
    quotes: List[QuoteObs],
    ts: datetime,
    horizon_seconds: int,
) -> Optional[Tuple[datetime, float]]:
    if not quotes:
        return None
    target = ts.timestamp() + horizon_seconds
    times = [q.ts.timestamp() for q in quotes]
    idx = bisect_left(times, target)
    if idx >= len(quotes):
        return None
    return quotes[idx].ts, quotes[idx].mid


def parse_log(path: str) -> Tuple[Dict[str, List[QuoteObs]], List[FillObs]]:
    quotes_by_token: Dict[str, List[QuoteObs]] = {}
    fills: List[FillObs] = []

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            quote_match = QUOTE_RE.search(line)
            if quote_match:
                ts = _parse_ts(quote_match.group("ts"))
                token = quote_match.group("token")
                mid_str = quote_match.group("mid")
                if mid_str == "N/A":
                    continue
                quote = QuoteObs(
                    ts=ts,
                    mid=float(mid_str),
                    best_bid=float(quote_match.group("best_bid")),
                    best_ask=float(quote_match.group("best_ask")),
                )
                quotes_by_token.setdefault(token, []).append(quote)
                continue

            fill_match = FILL_RE.search(line)
            if fill_match:
                ts = _parse_ts(fill_match.group("ts"))
                fill = FillObs(
                    ts=ts,
                    token=fill_match.group("token"),
                    side=fill_match.group("side"),
                    price=float(fill_match.group("price")),
                    size=float(fill_match.group("size")),
                )
                fills.append(fill)

    return quotes_by_token, fills


def summarize(
    quotes_by_token: Dict[str, List[QuoteObs]],
    fills: List[FillObs],
    lookback_seconds: int,
    horizons: List[int],
    csv_path: Optional[str],
    token_filter: Optional[str],
) -> None:
    rows = []
    by_token_stats: Dict[str, Dict[str, float]] = {}

    for fill in fills:
        if token_filter and fill.token != token_filter:
            continue
        quotes = quotes_by_token.get(fill.token, [])
        quote = _find_quote_before(quotes, fill.ts)
        if not quote:
            continue
        age = (fill.ts - quote.ts).total_seconds()
        if age > lookback_seconds:
            continue

        edge = _edge_bps(fill.side, fill.price, quote.mid)
        markouts = {}
        for horizon in horizons:
            future = _find_mid_after(quotes, fill.ts, horizon)
            if not future:
                markouts[horizon] = None
                continue
            _, future_mid = future
            markouts[horizon] = _markout_bps(fill.side, fill.price, future_mid)

        rows.append({
            "timestamp": fill.ts.isoformat(),
            "token": fill.token,
            "side": fill.side,
            "price": fill.price,
            "size": fill.size,
            "mid_at_fill": quote.mid,
            "edge_bps": edge,
            "quote_age_s": age,
            **{f"markout_{h}s_bps": markouts[h] for h in horizons},
        })

        stats = by_token_stats.setdefault(
            fill.token,
            {
                "fills": 0,
                "edge_bps_sum": 0.0,
                "edge_bps_count": 0,
                "adverse_count": 0,
            },
        )
        stats["fills"] += 1
        if edge is not None:
            stats["edge_bps_sum"] += edge
            stats["edge_bps_count"] += 1
            if edge < 0:
                stats["adverse_count"] += 1

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            fieldnames = [
                "timestamp",
                "token",
                "side",
                "price",
                "size",
                "mid_at_fill",
                "edge_bps",
                "quote_age_s",
                *[f"markout_{h}s_bps" for h in horizons],
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    total_fills = sum(stats["fills"] for stats in by_token_stats.values())
    print("=" * 60)
    print(f"Analyzed fills: {total_fills}")
    print(f"Tokens: {len(by_token_stats)}")
    print("=" * 60)

    for token, stats in sorted(by_token_stats.items()):
        avg_edge = (
            stats["edge_bps_sum"] / stats["edge_bps_count"]
            if stats["edge_bps_count"]
            else 0.0
        )
        adverse_rate = (
            stats["adverse_count"] / stats["edge_bps_count"] * 100
            if stats["edge_bps_count"]
            else 0.0
        )
        print(
            f"{token} | fills={stats['fills']} "
            f"avg_edge_bps={avg_edge:+.1f} "
            f"adverse_rate={adverse_rate:.1f}%"
        )
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze AQ logs for adverse selection and markouts."
    )
    parser.add_argument("--log", required=True, help="Path to log file")
    parser.add_argument("--lookback", type=int, default=30,
                        help="Max seconds between quote and fill")
    parser.add_argument("--horizons", default="10,30,60",
                        help="Comma-separated markout horizons in seconds")
    parser.add_argument("--csv", help="Optional path to write per-fill CSV")
    parser.add_argument("--token", help="Filter to a specific token prefix")
    args = parser.parse_args()

    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    quotes_by_token, fills = parse_log(args.log)
    summarize(
        quotes_by_token=quotes_by_token,
        fills=fills,
        lookback_seconds=args.lookback,
        horizons=horizons,
        csv_path=args.csv,
        token_filter=args.token,
    )


if __name__ == "__main__":
    main()
