"""
Unit tests for rebates/gabagool/scanner.py

Tests:
- GabagoolScanner._calculate_vwap(): Volume-weighted average price calculation
- GabagoolScanner._get_best_ask(): Best ask extraction with VWAP
- GabagoolScanner.scan_market(): Opportunity detection
- GabagoolScanner.should_execute(): Execution decision making
- Opportunity dataclass: Data structure validation
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.scanner import GabagoolScanner, Opportunity


class TestCalculateVWAP:
    """Tests for GabagoolScanner._calculate_vwap()."""

    def test_calculates_vwap_correctly(self):
        """Should calculate volume-weighted average price."""
        scanner = GabagoolScanner()

        orders = [
            {"price": "0.45", "size": "100"},
            {"price": "0.46", "size": "200"},
            {"price": "0.47", "size": "100"},
        ]

        # Target 150 shares
        # Takes 100 @ 0.45 = $45
        # Takes 50 @ 0.46 = $23
        # Total: 150 shares, $68 value
        # VWAP = 68 / 150 = 0.4533...
        vwap, size = scanner._calculate_vwap(orders, target_size=150, min_order_size=0)

        assert size == 150.0
        assert abs(vwap - 0.4533) < 0.001

    def test_filters_small_orders(self):
        """Should filter out small orders (potential spoofing)."""
        scanner = GabagoolScanner()

        orders = [
            {"price": "0.40", "size": "5"},  # $2 value - too small
            {"price": "0.45", "size": "100"},  # $45 value - valid
            {"price": "0.46", "size": "200"},  # $92 value - valid
        ]

        # With min_order_size=5, the first order ($2 value) should be skipped
        vwap, size = scanner._calculate_vwap(orders, target_size=100, min_order_size=5)

        # Should take from 0.45, not 0.40
        assert vwap == 0.45
        assert size == 100.0

    def test_returns_none_for_empty_orders(self):
        """Should return None for empty order list."""
        scanner = GabagoolScanner()

        vwap, size = scanner._calculate_vwap([], target_size=100)

        assert vwap is None
        assert size == 0.0

    def test_handles_insufficient_liquidity(self):
        """Should return available size when target exceeds liquidity."""
        scanner = GabagoolScanner()

        orders = [
            {"price": "0.45", "size": "50"},
            {"price": "0.46", "size": "30"},
        ]

        # Request 100 but only 80 available
        vwap, size = scanner._calculate_vwap(orders, target_size=100, min_order_size=0)

        assert size == 80.0
        # VWAP = (50*0.45 + 30*0.46) / 80 = (22.5 + 13.8) / 80 = 0.45375
        assert abs(vwap - 0.45375) < 0.001


class TestGetBestAsk:
    """Tests for GabagoolScanner._get_best_ask()."""

    def test_returns_best_ask_sorted(self):
        """Should return lowest ask price."""
        scanner = GabagoolScanner(trade_size=50)

        orderbook = {
            "asks": [
                {"price": "0.55", "size": "100"},
                {"price": "0.50", "size": "200"},  # Best ask
                {"price": "0.52", "size": "150"},
            ]
        }

        price, size = scanner._get_best_ask(orderbook)

        # Should get price starting from 0.50 (lowest)
        assert price == 0.50
        assert size == 50.0

    def test_returns_none_for_no_asks(self):
        """Should return None when no asks."""
        scanner = GabagoolScanner()

        orderbook = {"asks": []}

        price, size = scanner._get_best_ask(orderbook)

        assert price is None
        assert size == 0.0


class TestScanMarket:
    """Tests for GabagoolScanner.scan_market()."""

    @patch("rebates.gabagool.scanner.requests.get")
    def test_detects_opportunity_below_threshold(self, mock_get):
        """Should detect opportunity when combined cost < threshold."""
        scanner = GabagoolScanner(
            profit_threshold=0.99,
            min_liquidity=10,
            min_net_profit_pct=0.1,
            trade_size=100,
        )

        # Mock orderbook responses
        def mock_response(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "up_token" in url:
                resp.json.return_value = {
                    "asks": [{"price": "0.48", "size": "200"}]
                }
            else:
                resp.json.return_value = {
                    "asks": [{"price": "0.49", "size": "200"}]
                }
            return resp

        mock_get.side_effect = mock_response

        opportunity = scanner.scan_market(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up_token",
            down_token="down_token",
            neg_risk=False,
        )

        # Combined cost = 0.48 + 0.49 = 0.97 < 0.99 threshold
        assert opportunity is not None
        assert abs(opportunity.combined_cost - 0.97) < 0.001
        assert opportunity.up_price == 0.48
        assert opportunity.down_price == 0.49
        assert abs(opportunity.gross_profit_pct - 3.0) < 0.01  # (1.00 - 0.97) * 100
        # max_size is min of what's available, bounded by trade_size for VWAP
        assert opportunity.max_size == 100.0

    @patch("rebates.gabagool.scanner.requests.get")
    def test_no_opportunity_above_threshold(self, mock_get):
        """Should not detect opportunity when combined cost >= threshold."""
        scanner = GabagoolScanner(
            profit_threshold=0.99,
            min_liquidity=10,
            trade_size=100,
        )

        def mock_response(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            # Combined = 0.50 + 0.51 = 1.01 > 0.99 threshold
            if "up_token" in url:
                resp.json.return_value = {
                    "asks": [{"price": "0.50", "size": "200"}]
                }
            else:
                resp.json.return_value = {
                    "asks": [{"price": "0.51", "size": "200"}]
                }
            return resp

        mock_get.side_effect = mock_response

        opportunity = scanner.scan_market(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up_token",
            down_token="down_token",
            neg_risk=False,
        )

        assert opportunity is None

    @patch("rebates.gabagool.scanner.requests.get")
    def test_no_opportunity_insufficient_liquidity(self, mock_get):
        """Should not detect opportunity when liquidity is insufficient."""
        scanner = GabagoolScanner(
            profit_threshold=0.99,
            min_liquidity=100,  # Require 100 shares
            trade_size=50,
        )

        def mock_response(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "up_token" in url:
                resp.json.return_value = {
                    "asks": [{"price": "0.45", "size": "200"}]
                }
            else:
                resp.json.return_value = {
                    "asks": [{"price": "0.45", "size": "50"}]  # Only 50 available
                }
            return resp

        mock_get.side_effect = mock_response

        opportunity = scanner.scan_market(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up_token",
            down_token="down_token",
            neg_risk=False,
        )

        # max_size = min(200, 50) = 50 < 100 min_liquidity
        assert opportunity is None

    @patch("rebates.gabagool.scanner.requests.get")
    def test_handles_api_error(self, mock_get):
        """Should return None on API error."""
        scanner = GabagoolScanner()

        mock_get.side_effect = Exception("Network error")

        opportunity = scanner.scan_market(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up_token",
            down_token="down_token",
            neg_risk=False,
        )

        assert opportunity is None

    @patch("rebates.gabagool.scanner.requests.get")
    def test_calculates_expected_profit_usd(self, mock_get):
        """Should calculate expected profit in USD correctly."""
        scanner = GabagoolScanner(
            profit_threshold=0.99,
            min_liquidity=10,
            min_net_profit_pct=0.1,
            trade_size=100,
        )

        def mock_response(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            # Combined = 0.48 + 0.49 = 0.97
            if "up_token" in url:
                resp.json.return_value = {
                    "asks": [{"price": "0.48", "size": "200"}]
                }
            else:
                resp.json.return_value = {
                    "asks": [{"price": "0.49", "size": "200"}]
                }
            return resp

        mock_get.side_effect = mock_response

        opportunity = scanner.scan_market(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up_token",
            down_token="down_token",
            neg_risk=False,
        )

        # Expected profit = trade_size * (1.00 - combined_cost) - gas
        # = 100 * (1.00 - 0.97) - 0.002
        # = 100 * 0.03 - 0.002 = 2.998
        assert opportunity is not None
        assert abs(opportunity.expected_profit_usd - 2.998) < 0.01


class TestShouldExecute:
    """Tests for GabagoolScanner.should_execute()."""

    def test_allows_execution_when_conditions_met(self):
        """Should allow execution when all conditions are met."""
        scanner = GabagoolScanner()

        opportunity = Opportunity(
            market_slug="test",
            condition_id="cond123",
            up_token="up",
            down_token="down",
            neg_risk=False,
            up_price=0.48,
            down_price=0.49,
            combined_cost=0.97,
            up_size=200,
            down_size=200,
            max_size=200,
            gross_profit_pct=3.0,
            net_profit_pct=2.9,
            expected_profit_usd=2.90,
            detected_at=datetime.now(timezone.utc),
            market_start_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            seconds_to_start=300,
        )

        should_exec, reason = scanner.should_execute(opportunity)

        assert should_exec is True
        assert reason == "OK"

    def test_blocks_execution_too_close_to_start(self):
        """Should block execution when too close to market start."""
        scanner = GabagoolScanner()

        opportunity = Opportunity(
            market_slug="test",
            condition_id="cond123",
            up_token="up",
            down_token="down",
            neg_risk=False,
            up_price=0.48,
            down_price=0.49,
            combined_cost=0.97,
            up_size=200,
            down_size=200,
            max_size=200,
            gross_profit_pct=3.0,
            net_profit_pct=2.9,
            expected_profit_usd=2.90,
            detected_at=datetime.now(timezone.utc),
            market_start_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            seconds_to_start=30,  # Less than MIN_TIME_TO_START (60)
        )

        should_exec, reason = scanner.should_execute(opportunity)

        assert should_exec is False
        assert "Too close to start" in reason

    def test_blocks_execution_insufficient_liquidity(self):
        """Should block execution when liquidity is too low."""
        scanner = GabagoolScanner()

        opportunity = Opportunity(
            market_slug="test",
            condition_id="cond123",
            up_token="up",
            down_token="down",
            neg_risk=False,
            up_price=0.48,
            down_price=0.49,
            combined_cost=0.97,
            up_size=5,
            down_size=5,
            max_size=5,  # Below MIN_ORDER_SIZE (10)
            gross_profit_pct=3.0,
            net_profit_pct=2.9,
            expected_profit_usd=0.15,
            detected_at=datetime.now(timezone.utc),
            seconds_to_start=300,
        )

        should_exec, reason = scanner.should_execute(opportunity)

        assert should_exec is False
        assert "Insufficient liquidity" in reason


class TestOpportunityDataclass:
    """Tests for Opportunity dataclass."""

    def test_creates_opportunity_with_all_fields(self):
        """Should create opportunity with all required fields."""
        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=10)

        opp = Opportunity(
            market_slug="btc-updown-15m-123",
            condition_id="0x123abc",
            up_token="token_up",
            down_token="token_down",
            neg_risk=True,
            up_price=0.48,
            down_price=0.49,
            combined_cost=0.97,
            up_size=150,
            down_size=200,
            max_size=150,
            gross_profit_pct=3.0,
            net_profit_pct=2.9,
            expected_profit_usd=2.90,
            detected_at=now,
            market_start_time=start,
            seconds_to_start=600,
        )

        assert opp.market_slug == "btc-updown-15m-123"
        assert opp.neg_risk is True
        assert opp.combined_cost == 0.97
        assert opp.max_size == 150


class TestScanMarkets:
    """Tests for GabagoolScanner.scan_markets()."""

    @patch("rebates.gabagool.scanner.requests.get")
    def test_scans_multiple_markets(self, mock_get):
        """Should scan multiple markets and return sorted opportunities."""
        scanner = GabagoolScanner(
            profit_threshold=0.99,
            min_liquidity=10,
            min_net_profit_pct=0.1,
            trade_size=100,
        )

        call_count = [0]

        def mock_response(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            call_count[0] += 1

            # Market 1: 3% profit
            if "market1_up" in url:
                resp.json.return_value = {"asks": [{"price": "0.48", "size": "200"}]}
            elif "market1_down" in url:
                resp.json.return_value = {"asks": [{"price": "0.49", "size": "200"}]}
            # Market 2: 5% profit (higher)
            elif "market2_up" in url:
                resp.json.return_value = {"asks": [{"price": "0.47", "size": "200"}]}
            elif "market2_down" in url:
                resp.json.return_value = {"asks": [{"price": "0.48", "size": "200"}]}
            # Market 3: Above threshold (no opportunity)
            elif "market3" in url:
                resp.json.return_value = {"asks": [{"price": "0.52", "size": "200"}]}
            else:
                resp.json.return_value = {"asks": []}

            return resp

        mock_get.side_effect = mock_response

        markets = [
            {"slug": "market1", "conditionId": "c1", "up_token": "market1_up", "down_token": "market1_down", "neg_risk": False},
            {"slug": "market2", "conditionId": "c2", "up_token": "market2_up", "down_token": "market2_down", "neg_risk": False},
            {"slug": "market3", "conditionId": "c3", "up_token": "market3_up", "down_token": "market3_down", "neg_risk": False},
        ]

        opportunities = scanner.scan_markets(markets)

        # Should find 2 opportunities (market3 above threshold)
        assert len(opportunities) == 2

        # Should be sorted by profit (highest first)
        assert opportunities[0].market_slug == "market2"  # 5% profit
        assert opportunities[1].market_slug == "market1"  # 3% profit
