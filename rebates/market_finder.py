"""
Market finder for 15-minute crypto Up/Down markets.

Discovers upcoming markets by generating slug patterns based on timestamps
and querying the Gamma API.
"""
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from .config import (
    GAMMA_API_BASE,
    SLUG_PATTERN,
    ASSETS,
    SAFETY_BUFFER_SECONDS,
)


class CryptoMarketFinder:
    """Find 15-minute crypto Up/Down markets from Polymarket API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })

    def _get_upcoming_timestamps(self, count: int = 4) -> List[int]:
        """
        Generate timestamps for upcoming 15-minute slots.

        Markets are created at :00, :15, :30, :45 minutes past the hour.
        The timestamp represents the END of the 15-minute window.
        """
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        # Round up to the next 15-minute boundary
        slot_duration = 15 * 60  # 15 minutes in seconds
        next_slot_end = ((current_ts // slot_duration) + 1) * slot_duration

        timestamps = []
        for i in range(count):
            timestamps.append(next_slot_end + (i * slot_duration))

        return timestamps

    def _get_timestamps_including_current(self, count: int = 4) -> List[int]:
        """
        Generate timestamps including the current live slot.

        Returns timestamps for:
        - Current live market (if any)
        - Next upcoming markets
        """
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        slot_duration = 15 * 60  # 15 minutes in seconds

        # Get the END timestamp of the CURRENT slot (the live market)
        current_slot_end = ((current_ts // slot_duration) + 1) * slot_duration

        timestamps = []
        for i in range(count):
            timestamps.append(current_slot_end + (i * slot_duration))

        return timestamps

    def _fetch_market(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch a single market by slug."""
        try:
            url = f"{GAMMA_API_BASE}/markets?slug={slug}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    return data[0]
        except Exception as e:
            print(f"Error fetching market {slug}: {e}")
        return None

    def _parse_event_start_time(self, market: Dict[str, Any]) -> Optional[datetime]:
        """Parse the eventStartTime from market data."""
        event_start = market.get("eventStartTime")
        if not event_start:
            return None
        try:
            # Handle format: "2026-01-09T23:45:00Z" or similar
            if event_start.endswith("Z"):
                event_start = event_start[:-1] + "+00:00"
            return datetime.fromisoformat(event_start)
        except Exception:
            return None

    def is_safe_to_trade(self, market: Dict[str, Any]) -> bool:
        """
        CRITICAL: Returns True only if market is UPCOMING.
        Returns False if market is LIVE or about to go live.

        Multiple safety checks:
        1. eventStartTime must be in the future
        2. Must have sufficient safety buffer
        3. Market must be accepting orders
        """
        now = datetime.now(timezone.utc)

        # Check 1: Parse event start time
        start_time = self._parse_event_start_time(market)
        if start_time is None:
            print(f"BLOCKED: Cannot parse eventStartTime for {market.get('slug', 'unknown')}")
            return False

        # Check 2: Market must not have started
        if start_time <= now:
            print(f"BLOCKED: Market already started at {start_time}")
            return False

        # Check 3: Must have safety buffer
        buffer = timedelta(seconds=SAFETY_BUFFER_SECONDS)
        if start_time <= now + buffer:
            time_until = (start_time - now).total_seconds()
            print(f"BLOCKED: Market starts in {time_until:.0f}s (buffer: {SAFETY_BUFFER_SECONDS}s)")
            return False

        # Check 4: Market must be accepting orders
        if not market.get("acceptingOrders", False):
            print(f"BLOCKED: Market not accepting orders")
            return False

        # Check 5: Market must not be closed
        if market.get("closed", False):
            print(f"BLOCKED: Market is closed")
            return False

        return True

    def is_tradeable_for_active_quoting(self, market: Dict[str, Any]) -> bool:
        """
        Returns True if market is valid for active quoting.

        For active quoting, we WANT to trade on:
        - Currently live markets (started but not ended)
        - Upcoming markets

        We reject:
        - Resolved/closed markets
        - Markets not accepting orders
        """
        now = datetime.now(timezone.utc)

        # Check 1: Parse end time to determine if market has resolved
        end_date_str = market.get("endDate")
        if end_date_str:
            try:
                if end_date_str.endswith("Z"):
                    end_date_str = end_date_str[:-1] + "+00:00"
                end_time = datetime.fromisoformat(end_date_str)
                if now >= end_time:
                    # Market has ended
                    return False
            except Exception:
                pass

        # Check 2: Market must be accepting orders
        if not market.get("acceptingOrders", False):
            return False

        # Check 3: Market must not be closed
        if market.get("closed", False):
            return False

        return True

    def get_upcoming_markets(self) -> List[Dict[str, Any]]:
        """
        Find all upcoming 15-minute markets for configured assets.

        Returns markets that:
        1. Are 15-minute crypto markets (BTC, ETH, SOL)
        2. Have eventStartTime in the future
        3. Are safe to trade (start > now + buffer)
        """
        markets = []
        timestamps = self._get_upcoming_timestamps(count=4)

        for timestamp in timestamps:
            for asset in ASSETS:
                slug = SLUG_PATTERN.format(asset=asset, timestamp=timestamp)
                market = self._fetch_market(slug)

                if market and self.is_safe_to_trade(market):
                    # Add parsed timing info
                    market["_event_start"] = self._parse_event_start_time(market)
                    market["_asset"] = asset.upper()
                    markets.append(market)

        # Sort by event start time (earliest first)
        markets.sort(key=lambda m: m.get("_event_start") or datetime.max.replace(tzinfo=timezone.utc))

        return markets

    def get_live_and_upcoming_markets(self, count: int = 4) -> List[Dict[str, Any]]:
        """
        Find live and upcoming 15-minute markets for active quoting.

        Unlike get_upcoming_markets, this INCLUDES currently live markets.

        Returns markets that:
        1. Are 15-minute crypto markets
        2. Have not yet resolved (end time in future)
        3. Are accepting orders and not closed
        """
        markets = []
        timestamps = self._get_timestamps_including_current(count=count)

        for timestamp in timestamps:
            for asset in ASSETS:
                slug = SLUG_PATTERN.format(asset=asset, timestamp=timestamp)
                market = self._fetch_market(slug)

                if market and self.is_tradeable_for_active_quoting(market):
                    # Add parsed timing info
                    market["_event_start"] = self._parse_event_start_time(market)
                    market["_asset"] = asset.upper()
                    markets.append(market)

        # Sort by event start time (earliest first)
        markets.sort(key=lambda m: m.get("_event_start") or datetime.max.replace(tzinfo=timezone.utc))

        return markets

    def get_next_market(self, asset: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get the next upcoming market to trade.

        Args:
            asset: Optional asset filter (e.g., "btc", "eth", "sol")
        """
        markets = self.get_upcoming_markets()

        if asset:
            markets = [m for m in markets if m.get("_asset", "").lower() == asset.lower()]

        return markets[0] if markets else None

    def get_market_info(self, market: Dict[str, Any]) -> str:
        """Format market info for logging."""
        question = market.get("question", "Unknown")
        event_start = market.get("_event_start")
        now = datetime.now(timezone.utc)

        if event_start:
            time_until = (event_start - now).total_seconds()
            return f"{question} (starts in {time_until:.0f}s)"
        return question
