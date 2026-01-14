"""
Unit tests for rebates/gabagool/circuit_breaker.py

Tests:
- CircuitBreakerConfig: Configuration dataclass defaults and customization
- CircuitBreakerState: State tracking and initialization
- CircuitBreaker.check_can_trade(): Trade permission checking
- CircuitBreaker.record_trade_result(): Trade result recording
- CircuitBreaker.record_position_closed(): Position close tracking
- Auto-recovery and manual reset behavior
- Daily P&L reset
- Hourly error count reset
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
)


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


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig dataclass."""

    def test_default_values(self):
        """Should have sensible default values."""
        config = CircuitBreakerConfig()

        assert config.max_position_per_market == 500.0
        assert config.max_total_position == 2000.0
        assert config.max_daily_loss == 100.0
        assert config.max_loss_per_trade == 20.0
        assert config.max_consecutive_errors == 5
        assert config.max_errors_per_hour == 20
        assert config.cooldown_seconds == 300
        assert config.auto_recover is True
        assert config.require_manual_reset is False

    def test_custom_values(self):
        """Should accept custom values."""
        config = CircuitBreakerConfig(
            max_position_per_market=1000.0,
            max_total_position=5000.0,
            max_daily_loss=200.0,
            max_consecutive_errors=10,
            cooldown_seconds=600,
            auto_recover=False,
        )

        assert config.max_position_per_market == 1000.0
        assert config.max_total_position == 5000.0
        assert config.max_daily_loss == 200.0
        assert config.max_consecutive_errors == 10
        assert config.cooldown_seconds == 600
        assert config.auto_recover is False


class TestCircuitBreakerState:
    """Tests for CircuitBreakerState dataclass."""

    def test_default_initialization(self):
        """Should initialize with correct defaults."""
        state = CircuitBreakerState()

        assert state.is_halted is False
        assert state.halt_reason == ""
        assert state.halt_time is None
        assert state.positions_by_market == {}
        assert state.total_position == 0.0
        assert state.daily_pnl == 0.0
        assert state.consecutive_errors == 0
        assert state.errors_this_hour == 0
        assert state.total_trades == 0
        assert state.successful_trades == 0
        assert state.failed_trades == 0


class TestCircuitBreakerCheckCanTrade:
    """Tests for CircuitBreaker.check_can_trade()."""

    def test_allows_trade_when_under_limits(self):
        """Should allow trades when all limits are respected."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is True
        assert reason == "OK"

    def test_blocks_trade_when_halted(self):
        """Should block trades when circuit breaker is halted."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.is_halted = True
        cb.state.halt_reason = "Manual halt"
        cb.state.halt_time = datetime.now(timezone.utc)

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert "Circuit breaker halted" in reason

    def test_blocks_trade_when_exceeds_market_limit(self):
        """Should block trade that would exceed market position limit."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.positions_by_market["market_1"] = 450.0

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert "market position limit" in reason

    def test_blocks_trade_when_exceeds_total_limit(self):
        """Should block trade that would exceed total position limit."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.total_position = 1950.0

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert "total position limit" in reason

    def test_blocks_and_halts_on_daily_loss_exceeded(self):
        """Should halt and block when daily loss limit is exceeded."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.daily_pnl = -150.0  # Exceeds default 100 limit

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert cb.state.is_halted is True
        assert "Daily loss limit" in cb.state.halt_reason

    def test_allows_trade_at_exact_market_limit(self):
        """Should allow trade that brings us exactly to the limit."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.positions_by_market["market_1"] = 400.0

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is True
        assert reason == "OK"

    def test_allows_new_market_position(self):
        """Should allow position in new market."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.positions_by_market["market_1"] = 400.0

        allowed, reason = run_async(cb.check_can_trade("market_2", 300.0))

        assert allowed is True
        assert reason == "OK"


class TestCircuitBreakerRecordTradeResult:
    """Tests for CircuitBreaker.record_trade_result()."""

    def test_records_successful_trade(self):
        """Should update state for successful trade."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        run_async(
            cb.record_trade_result(
                market_id="market_1",
                size=100.0,
                pnl=5.0,
                success=True,
            )
        )

        assert cb.state.total_trades == 1
        assert cb.state.successful_trades == 1
        assert cb.state.failed_trades == 0
        assert cb.state.positions_by_market["market_1"] == 100.0
        assert cb.state.total_position == 100.0
        assert cb.state.daily_pnl == 5.0
        assert cb.state.consecutive_errors == 0

    def test_records_failed_trade(self):
        """Should update error counts for failed trade."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        run_async(
            cb.record_trade_result(
                market_id="market_1",
                size=100.0,
                pnl=0.0,
                success=False,
                error_msg="Connection timeout",
            )
        )

        assert cb.state.total_trades == 1
        assert cb.state.successful_trades == 0
        assert cb.state.failed_trades == 1
        assert cb.state.consecutive_errors == 1
        assert cb.state.errors_this_hour == 1
        # Position should not change on failure
        assert cb.state.total_position == 0.0

    def test_resets_consecutive_errors_on_success(self):
        """Should reset consecutive errors after successful trade."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.consecutive_errors = 3

        run_async(
            cb.record_trade_result(
                market_id="market_1",
                size=100.0,
                pnl=2.0,
                success=True,
            )
        )

        assert cb.state.consecutive_errors == 0

    def test_halts_on_consecutive_errors(self):
        """Should halt after too many consecutive errors."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        for i in range(5):
            run_async(
                cb.record_trade_result(
                    market_id="market_1",
                    size=100.0,
                    pnl=0.0,
                    success=False,
                )
            )

        assert cb.state.is_halted is True
        assert "consecutive errors" in cb.state.halt_reason

    def test_halts_on_hourly_error_limit(self):
        """Should halt after too many errors in one hour."""
        config = CircuitBreakerConfig(max_errors_per_hour=5)
        cb = CircuitBreaker(config)

        for i in range(5):
            # Reset consecutive to avoid triggering that limit
            cb.state.consecutive_errors = 0
            run_async(
                cb.record_trade_result(
                    market_id="market_1",
                    size=100.0,
                    pnl=0.0,
                    success=False,
                )
            )

        assert cb.state.is_halted is True
        assert "errors this hour" in cb.state.halt_reason

    def test_halts_on_anomalous_loss(self):
        """Should halt on large loss even if trade marked successful."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        run_async(
            cb.record_trade_result(
                market_id="market_1",
                size=100.0,
                pnl=-50.0,  # Exceeds max_loss_per_trade (20)
                success=True,
            )
        )

        assert cb.state.is_halted is True
        assert "Anomalous loss" in cb.state.halt_reason


class TestCircuitBreakerRecordPositionClosed:
    """Tests for CircuitBreaker.record_position_closed()."""

    def test_reduces_position_on_close(self):
        """Should reduce position when closing."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.positions_by_market["market_1"] = 200.0
        cb.state.total_position = 200.0

        run_async(
            cb.record_position_closed(
                market_id="market_1",
                size=100.0,
                pnl=10.0,
            )
        )

        assert cb.state.positions_by_market["market_1"] == 100.0
        assert cb.state.total_position == 100.0
        assert cb.state.daily_pnl == 10.0

    def test_does_not_go_negative(self):
        """Should not allow negative positions."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.positions_by_market["market_1"] = 200.0
        cb.state.total_position = 200.0

        run_async(
            cb.record_position_closed(
                market_id="market_1",
                size=300.0,  # More than we have
                pnl=5.0,
            )
        )

        assert cb.state.positions_by_market["market_1"] == 0.0
        assert cb.state.total_position == 0.0


class TestCircuitBreakerAutoRecovery:
    """Tests for auto-recovery behavior."""

    def test_auto_recovers_after_cooldown(self):
        """Should auto-recover after cooldown period."""
        config = CircuitBreakerConfig(
            cooldown_seconds=1,  # 1 second for testing
            auto_recover=True,
        )
        cb = CircuitBreaker(config)

        # Halt the circuit breaker
        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc) - timedelta(seconds=2)

        # Should auto-recover on next check
        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is True
        assert cb.state.is_halted is False

    def test_does_not_auto_recover_when_disabled(self):
        """Should not auto-recover when disabled."""
        config = CircuitBreakerConfig(
            cooldown_seconds=1,
            auto_recover=False,
        )
        cb = CircuitBreaker(config)

        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc) - timedelta(seconds=2)

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert cb.state.is_halted is True

    def test_does_not_auto_recover_when_manual_reset_required(self):
        """Should not auto-recover when manual reset is required."""
        config = CircuitBreakerConfig(
            cooldown_seconds=1,
            auto_recover=True,
            require_manual_reset=True,
        )
        cb = CircuitBreaker(config)

        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc) - timedelta(seconds=2)

        allowed, reason = run_async(cb.check_can_trade("market_1", 100.0))

        assert allowed is False
        assert cb.state.is_halted is True


class TestCircuitBreakerManualReset:
    """Tests for manual reset."""

    def test_manual_reset_clears_halt(self):
        """Should clear halt on manual reset."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc)
        cb.state.consecutive_errors = 5

        run_async(cb.manual_reset())

        assert cb.state.is_halted is False
        assert cb.state.halt_reason == ""
        assert cb.state.halt_time is None
        assert cb.state.consecutive_errors == 0


class TestCircuitBreakerGetStatus:
    """Tests for get_status()."""

    def test_returns_complete_status(self):
        """Should return all status fields."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.daily_pnl = 50.0
        cb.state.total_trades = 10
        cb.state.successful_trades = 8
        cb.state.failed_trades = 2

        status = cb.get_status()

        assert "is_halted" in status
        assert "halt_reason" in status
        assert "total_position" in status
        assert "positions_by_market" in status
        assert "daily_pnl" in status
        assert "consecutive_errors" in status
        assert "total_trades" in status
        assert "success_rate" in status
        assert status["daily_pnl"] == 50.0
        assert status["total_trades"] == 10
        assert status["success_rate"] == 80.0


class TestCircuitBreakerDailyReset:
    """Tests for daily P&L reset."""

    def test_resets_daily_pnl_at_midnight(self):
        """Should reset daily P&L when date changes."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.daily_pnl = 100.0
        # Set reset time to yesterday
        cb.state.daily_pnl_reset_time = datetime.now(timezone.utc) - timedelta(days=1)

        # This should trigger the reset
        run_async(cb.check_can_trade("market_1", 10.0))

        assert cb.state.daily_pnl == 0.0


class TestCircuitBreakerHourlyReset:
    """Tests for hourly error count reset."""

    def test_resets_hourly_errors_after_hour(self):
        """Should reset hourly error count after an hour."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.errors_this_hour = 15
        # Set hour start to 2 hours ago
        cb.state.error_hour_start = datetime.now(timezone.utc) - timedelta(hours=2)

        # This should trigger the reset
        run_async(cb.check_can_trade("market_1", 10.0))

        assert cb.state.errors_this_hour == 0


class TestCircuitBreakerForceHalt:
    """Tests for force_halt()."""

    def test_force_halt_sets_halt_state(self):
        """Should set halt state when force halted."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        run_async(cb.force_halt("Manual intervention required"))

        assert cb.state.is_halted is True
        assert cb.state.halt_reason == "Manual intervention required"
        assert cb.state.halt_time is not None
