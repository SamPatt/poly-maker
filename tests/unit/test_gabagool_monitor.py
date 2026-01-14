"""
Unit tests for rebates/gabagool/monitor.py

Tests:
- GabagoolMonitor._extract_tokens(): Token ID extraction
- GabagoolMonitor._prepare_market_for_scan(): Market preparation
- GabagoolMonitor.scan_once(): Single scan cycle
- GabagoolMonitor.get_status(): Status reporting
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import json

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.monitor import GabagoolMonitor
from rebates.gabagool.scanner import Opportunity
from rebates.gabagool.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


def run_async(coro):
    """Helper to run async coroutines in sync tests."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TestExtractTokens:
    """Tests for GabagoolMonitor._extract_tokens()."""

    def test_extracts_tokens_from_list(self):
        """Should extract tokens from list format."""
        monitor = GabagoolMonitor()

        market = {
            "slug": "test-market",
            "clobTokenIds": ["token_up", "token_down"],
        }

        up, down = monitor._extract_tokens(market)

        assert up == "token_up"
        assert down == "token_down"

    def test_extracts_tokens_from_json_string(self):
        """Should extract tokens from JSON string format."""
        monitor = GabagoolMonitor()

        market = {
            "slug": "test-market",
            "clobTokenIds": '["token_up_json", "token_down_json"]',
        }

        up, down = monitor._extract_tokens(market)

        assert up == "token_up_json"
        assert down == "token_down_json"

    def test_returns_none_for_missing_tokens(self):
        """Should return None if tokens missing."""
        monitor = GabagoolMonitor()

        market = {
            "slug": "test-market",
            "clobTokenIds": ["only_one"],
        }

        up, down = monitor._extract_tokens(market)

        assert up is None
        assert down is None

    def test_returns_none_for_invalid_json(self):
        """Should return None for invalid JSON."""
        monitor = GabagoolMonitor()

        market = {
            "slug": "test-market",
            "clobTokenIds": "not valid json",
        }

        up, down = monitor._extract_tokens(market)

        assert up is None
        assert down is None


class TestIsNegRisk:
    """Tests for GabagoolMonitor._is_neg_risk()."""

    def test_detects_neg_risk_true(self):
        """Should detect negRisk=True."""
        monitor = GabagoolMonitor()

        assert monitor._is_neg_risk({"negRisk": True}) is True
        assert monitor._is_neg_risk({"neg_risk": True}) is True

    def test_detects_neg_risk_string(self):
        """Should detect negRisk='TRUE' and 'true'."""
        monitor = GabagoolMonitor()

        assert monitor._is_neg_risk({"negRisk": "TRUE"}) is True
        assert monitor._is_neg_risk({"negRisk": "true"}) is True

    def test_detects_not_neg_risk(self):
        """Should detect when not negative risk."""
        monitor = GabagoolMonitor()

        assert monitor._is_neg_risk({"negRisk": False}) is False
        assert monitor._is_neg_risk({}) is False


class TestPrepareMarketForScan:
    """Tests for GabagoolMonitor._prepare_market_for_scan()."""

    def test_prepares_valid_market(self):
        """Should prepare market with all required fields."""
        monitor = GabagoolMonitor()

        start_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        market = {
            "slug": "btc-updown-15m-123",
            "conditionId": "0xabc123",
            "clobTokenIds": ["token_up", "token_down"],
            "negRisk": True,
            "_event_start": start_time,
        }

        prepared = monitor._prepare_market_for_scan(market)

        assert prepared is not None
        assert prepared["slug"] == "btc-updown-15m-123"
        assert prepared["conditionId"] == "0xabc123"
        assert prepared["up_token"] == "token_up"
        assert prepared["down_token"] == "token_down"
        assert prepared["neg_risk"] is True
        assert prepared["start_time"] == start_time

    def test_returns_none_for_invalid_market(self):
        """Should return None if market cannot be prepared."""
        monitor = GabagoolMonitor()

        market = {
            "slug": "test",
            "clobTokenIds": [],  # No tokens
        }

        prepared = monitor._prepare_market_for_scan(market)

        assert prepared is None


class TestScanOnce:
    """Tests for GabagoolMonitor.scan_once()."""

    def test_scan_increments_counter(self):
        """Should increment scans_performed counter."""
        mock_finder = MagicMock()
        mock_finder.get_upcoming_markets.return_value = []

        cb = CircuitBreaker(CircuitBreakerConfig())
        monitor = GabagoolMonitor(
            market_finder=mock_finder,
            circuit_breaker=cb,
        )

        assert monitor.scans_performed == 0

        run_async(monitor.scan_once())

        assert monitor.scans_performed == 1

    def test_scan_respects_circuit_breaker(self):
        """Should not scan when circuit breaker is halted."""
        mock_finder = MagicMock()

        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc)

        monitor = GabagoolMonitor(
            market_finder=mock_finder,
            circuit_breaker=cb,
        )

        run_async(monitor.scan_once())

        # Should not call market finder when halted
        mock_finder.get_upcoming_markets.assert_not_called()

    def test_scan_returns_empty_when_no_markets(self):
        """Should return empty list when no markets found."""
        mock_finder = MagicMock()
        mock_finder.get_upcoming_markets.return_value = []

        monitor = GabagoolMonitor(market_finder=mock_finder)

        opportunities = run_async(monitor.scan_once())

        assert opportunities == []

    def test_scan_detects_opportunities(self):
        """Should detect and return opportunities."""
        mock_finder = MagicMock()
        start_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        mock_finder.get_upcoming_markets.return_value = [
            {
                "slug": "btc-updown-15m-123",
                "conditionId": "0xabc123",
                "clobTokenIds": ["token_up", "token_down"],
                "negRisk": False,
                "_event_start": start_time,
            }
        ]

        monitor = GabagoolMonitor(market_finder=mock_finder)

        # Mock the scanner's _fetch_all_orderbooks to return test data
        async def mock_fetch_all(token_ids):
            return {
                "token_up": {"asks": [{"price": "0.47", "size": "200"}]},
                "token_down": {"asks": [{"price": "0.48", "size": "200"}]},
            }

        monitor.scanner._fetch_all_orderbooks = mock_fetch_all

        opportunities = run_async(monitor.scan_once())

        # Combined = 0.47 + 0.48 = 0.95 < 0.99 threshold
        assert len(opportunities) == 1
        assert opportunities[0].market_slug == "btc-updown-15m-123"
        assert monitor.opportunities_detected == 1


class TestGetStatus:
    """Tests for GabagoolMonitor.get_status()."""

    def test_returns_complete_status(self):
        """Should return all status fields."""
        monitor = GabagoolMonitor()
        monitor.scans_performed = 10
        monitor.opportunities_detected = 3

        status = monitor.get_status()

        assert "running" in status
        assert "scans_performed" in status
        assert "opportunities_detected" in status
        assert "dry_run" in status
        assert "circuit_breaker" in status
        assert status["scans_performed"] == 10
        assert status["opportunities_detected"] == 3

    def test_includes_last_opportunity(self):
        """Should include last opportunity when available."""
        monitor = GabagoolMonitor()

        monitor.last_opportunity = Opportunity(
            market_slug="test-market",
            condition_id="cond123",
            up_token="up",
            down_token="down",
            neg_risk=False,
            up_price=0.48,
            down_price=0.49,
            combined_cost=0.97,
            up_size=100,
            down_size=100,
            max_size=100,
            gross_profit_pct=3.0,
            net_profit_pct=2.9,
            expected_profit_usd=2.90,
            detected_at=datetime.now(timezone.utc),
        )

        status = monitor.get_status()

        assert status["last_opportunity"] is not None
        assert status["last_opportunity"]["market"] == "test-market"
        assert status["last_opportunity"]["profit_pct"] == 3.0


class TestMonitorStop:
    """Tests for GabagoolMonitor.stop()."""

    def test_stop_sets_running_false(self):
        """Should set running to False."""
        monitor = GabagoolMonitor()
        monitor.running = True

        monitor.stop()

        assert monitor.running is False
