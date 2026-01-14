"""
Unit tests for rebates/gabagool/executor.py

Tests:
- GabagoolExecutor: Order execution
- TrackedPosition: Position tracking
- ExecutionResult: Result handling
- Dry run behavior
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.executor import (
    GabagoolExecutor,
    ExecutionResult,
    TrackedPosition,
    ExecutionStrategy,
)
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


def make_opportunity(
    market_slug="test-market",
    up_price=0.48,
    down_price=0.49,
    combined_cost=0.97,
) -> Opportunity:
    """Create a test opportunity."""
    return Opportunity(
        market_slug=market_slug,
        condition_id="0xcond123",
        up_token="token_up_123",
        down_token="token_down_123",
        neg_risk=False,
        up_price=up_price,
        down_price=down_price,
        combined_cost=combined_cost,
        up_size=100.0,
        down_size=100.0,
        max_size=100.0,
        gross_profit_pct=(1.0 - combined_cost) * 100,
        net_profit_pct=2.9,
        expected_profit_usd=2.90,
        detected_at=datetime.now(timezone.utc),
    )


class TestTrackedPosition:
    """Tests for TrackedPosition dataclass."""

    def test_min_filled_returns_minimum(self):
        """Should return minimum of up and down filled."""
        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="cond",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=40,
            down_filled=50,
        )

        assert position.min_filled == 40

    def test_imbalance_calculates_difference(self):
        """Should calculate absolute difference between sides."""
        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="cond",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=40,
            down_filled=50,
        )

        assert position.imbalance == 10

    def test_imbalance_side_returns_excess_side(self):
        """Should return which side has excess."""
        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="cond",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=50,
            down_filled=40,
        )

        assert position.imbalance_side == "up"

    def test_imbalance_side_returns_none_when_balanced(self):
        """Should return None when sides are balanced."""
        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="cond",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=50,
            down_filled=50,
            is_balanced=True,
        )

        assert position.imbalance_side is None


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""

    def test_default_values(self):
        """Should have sensible defaults."""
        result = ExecutionResult(success=True)

        assert result.success is True
        assert result.reason == ""
        assert result.up_filled == 0.0
        assert result.down_filled == 0.0
        assert result.total_cost == 0.0

    def test_stores_fill_details(self):
        """Should store fill details."""
        result = ExecutionResult(
            success=True,
            reason="Orders placed",
            up_filled=50,
            down_filled=50,
            up_price=0.48,
            down_price=0.49,
            total_cost=48.50,
            expected_profit=1.50,
        )

        assert result.up_filled == 50
        assert result.down_filled == 50
        assert result.total_cost == 48.50


class TestGabagoolExecutorInit:
    """Tests for GabagoolExecutor initialization."""

    def test_initializes_with_defaults(self):
        """Should initialize with default values."""
        executor = GabagoolExecutor()

        assert executor.client is None
        assert executor.circuit_breaker is None
        assert executor.strategy == ExecutionStrategy.HYBRID
        assert executor.executions_attempted == 0
        assert executor.active_positions == []

    def test_accepts_custom_strategy(self):
        """Should accept custom execution strategy."""
        executor = GabagoolExecutor(strategy=ExecutionStrategy.TAKER)

        assert executor.strategy == ExecutionStrategy.TAKER


class TestExecuteDryRun:
    """Tests for dry run execution."""

    def test_dry_run_returns_simulated_result(self):
        """Should return simulated success without placing orders."""
        executor = GabagoolExecutor(dry_run=True)
        opportunity = make_opportunity()

        result = run_async(executor.execute(opportunity, size=50))

        assert result.success is True
        assert "Dry run" in result.reason
        assert result.up_filled == 50
        assert result.down_filled == 50

    def test_dry_run_increments_counter(self):
        """Should increment executions_attempted in dry run."""
        executor = GabagoolExecutor(dry_run=True)
        opportunity = make_opportunity()

        run_async(executor.execute(opportunity))

        assert executor.executions_attempted == 1


class TestExecuteWithCircuitBreaker:
    """Tests for circuit breaker integration."""

    def test_respects_circuit_breaker_block(self):
        """Should not execute when circuit breaker blocks."""
        cb = CircuitBreaker(CircuitBreakerConfig())
        cb.state.is_halted = True
        cb.state.halt_reason = "Test halt"
        cb.state.halt_time = datetime.now(timezone.utc)

        executor = GabagoolExecutor(circuit_breaker=cb, dry_run=True)
        opportunity = make_opportunity()

        result = run_async(executor.execute(opportunity))

        assert result.success is False
        assert "Circuit breaker" in result.reason

    def test_executes_when_circuit_breaker_allows(self):
        """Should execute when circuit breaker allows."""
        cb = CircuitBreaker(CircuitBreakerConfig())

        executor = GabagoolExecutor(circuit_breaker=cb, dry_run=True)
        opportunity = make_opportunity()

        result = run_async(executor.execute(opportunity))

        assert result.success is True


class TestTakerExecution:
    """Tests for taker execution strategy."""

    def test_taker_places_both_orders(self):
        """Should place orders for both UP and DOWN."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123", "success": True}

        executor = GabagoolExecutor(client=mock_client, dry_run=False)
        opportunity = make_opportunity()

        result = run_async(executor._execute_taker(opportunity, size=50))

        # Should have called create_order twice (UP and DOWN)
        assert mock_client.create_order.call_count == 2

    def test_taker_handles_order_error(self):
        """Should handle order placement errors."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {
            "success": False,
            "errorMsg": "Insufficient balance"
        }

        executor = GabagoolExecutor(client=mock_client, dry_run=False)
        opportunity = make_opportunity()

        result = run_async(executor._execute_taker(opportunity, size=50))

        assert result.success is False
        assert "error" in result.reason.lower()


class TestMakerExecution:
    """Tests for maker execution strategy."""

    def test_maker_uses_post_only(self):
        """Should use post_only=True for maker orders."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        executor = GabagoolExecutor(client=mock_client, dry_run=False)
        opportunity = make_opportunity()

        # Run with short timeout to avoid long waits
        result = run_async(executor._execute_maker(opportunity, size=50, timeout=0.1))

        # Verify post_only was used
        calls = mock_client.create_order.call_args_list
        for call in calls:
            assert call.kwargs.get("post_only") is True

    def test_maker_rejects_unprofitable_prices(self):
        """Should reject if maker prices not profitable."""
        executor = GabagoolExecutor(dry_run=False)

        # Opportunity with combined cost that after tick adjustment is unprofitable
        # Maker prices = (up_price - 0.01) + (down_price - 0.01) = combined_cost - 0.02
        # For rejection: combined_cost - 0.02 >= 0.99, so combined_cost >= 1.01
        opportunity = make_opportunity(
            up_price=0.51,
            down_price=0.51,
            combined_cost=1.02,
        )

        result = run_async(executor._execute_maker(opportunity, size=50, timeout=0.1))

        assert result.success is False
        assert "not profitable" in result.reason.lower()


class TestMergePosition:
    """Tests for position merging."""

    def test_merge_dry_run(self):
        """Should simulate merge in dry run mode."""
        executor = GabagoolExecutor(dry_run=True)

        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="0xcond123",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=50,
            down_filled=50,
            is_balanced=True,
        )

        result = run_async(executor.merge_position(position))

        assert result is True
        assert position.is_closed is True
        assert position.realized_profit > 0

    def test_merge_rejects_unbalanced(self):
        """Should reject merging unbalanced positions."""
        executor = GabagoolExecutor(dry_run=True)

        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="cond",
            up_token="up",
            down_token="down",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=50,
            down_filled=40,
            is_balanced=False,
        )

        result = run_async(executor.merge_position(position))

        assert result is False

    def test_merge_calls_client(self):
        """Should call client.merge_positions with correct params."""
        mock_client = MagicMock()
        mock_client.merge_positions.return_value = "0xtxhash"

        executor = GabagoolExecutor(client=mock_client, dry_run=False)

        position = TrackedPosition(
            id="pos-1",
            market_slug="test",
            condition_id="0xcond123",
            up_token="up",
            down_token="down",
            neg_risk=True,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.49,
            combined_cost=0.97,
            target_size=50,
            up_filled=50,
            down_filled=50,
            is_balanced=True,
        )
        executor.active_positions.append(position)

        result = run_async(executor.merge_position(position))

        assert result is True
        mock_client.merge_positions.assert_called_once()
        call_kwargs = mock_client.merge_positions.call_args.kwargs
        assert call_kwargs["condition_id"] == "0xcond123"
        assert call_kwargs["is_neg_risk_market"] is True


class TestGetStatus:
    """Tests for executor status reporting."""

    def test_returns_complete_status(self):
        """Should return all status fields."""
        executor = GabagoolExecutor(dry_run=True, strategy=ExecutionStrategy.MAKER)
        executor.executions_attempted = 5
        executor.executions_successful = 3
        executor.total_profit = 10.50

        status = executor.get_status()

        assert status["dry_run"] is True
        assert status["strategy"] == "maker"
        assert status["executions_attempted"] == 5
        assert status["executions_successful"] == 3
        assert status["total_profit"] == 10.50


class TestExecutionStrategy:
    """Tests for execution strategy selection."""

    def test_uses_specified_strategy(self):
        """Should use the strategy passed to execute()."""
        executor = GabagoolExecutor(dry_run=True, strategy=ExecutionStrategy.MAKER)
        opportunity = make_opportunity()

        # Execute with TAKER strategy override
        result = run_async(executor.execute(opportunity, strategy=ExecutionStrategy.TAKER))

        # Should succeed (dry run always succeeds)
        assert result.success is True
