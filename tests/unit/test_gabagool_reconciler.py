"""
Unit tests for rebates/gabagool/reconciler.py

Tests:
- PositionReconciler: Partial fill handling
- ReconciliationResult: Result handling
- Rescue strategies
"""

import asyncio
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.reconciler import (
    PositionReconciler,
    PositionStatus,
    ReconciliationResult,
)
from rebates.gabagool.executor import TrackedPosition


def run_async(coro):
    """Helper to run async coroutines in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


def make_position(
    up_filled: float = 50,
    down_filled: float = 50,
    target_size: float = 50,
    is_balanced: bool = None,
) -> TrackedPosition:
    """Create a test position."""
    if is_balanced is None:
        is_balanced = abs(up_filled - down_filled) < 0.01

    return TrackedPosition(
        id="test-pos-1",
        market_slug="test-market",
        condition_id="0xcond123",
        up_token="token_up",
        down_token="token_down",
        neg_risk=False,
        entry_time=datetime.now(timezone.utc),
        up_entry_price=0.48,
        down_entry_price=0.49,
        combined_cost=0.97,
        target_size=target_size,
        up_filled=up_filled,
        down_filled=down_filled,
        is_balanced=is_balanced,
    )


class TestPositionStatus:
    """Tests for PositionStatus enum."""

    def test_all_statuses_defined(self):
        """Should have all expected statuses."""
        assert PositionStatus.PENDING.value == "pending"
        assert PositionStatus.PARTIALLY_FILLED.value == "partial"
        assert PositionStatus.FILLED.value == "filled"
        assert PositionStatus.MERGE_READY.value == "merge_ready"
        assert PositionStatus.MERGED.value == "merged"
        assert PositionStatus.CLOSED.value == "closed"


class TestReconciliationResult:
    """Tests for ReconciliationResult dataclass."""

    def test_creates_result(self):
        """Should create result with all fields."""
        result = ReconciliationResult(
            success=True,
            action_taken="rescue_taker",
            new_status=PositionStatus.MERGE_READY,
            details="Rescued 10 shares",
        )

        assert result.success is True
        assert result.action_taken == "rescue_taker"
        assert result.new_status == PositionStatus.MERGE_READY


class TestPositionReconcilerInit:
    """Tests for PositionReconciler initialization."""

    def test_initializes_with_defaults(self):
        """Should initialize with default values."""
        reconciler = PositionReconciler()

        assert reconciler.client is None
        assert reconciler.rescues_attempted == 0
        assert reconciler.rescues_successful == 0

    def test_accepts_custom_values(self):
        """Should accept custom configuration."""
        mock_client = MagicMock()
        reconciler = PositionReconciler(
            client=mock_client,
            max_imbalance_pct=30.0,
            rescue_timeout=5.0,
        )

        assert reconciler.client == mock_client
        assert reconciler.max_imbalance_pct == 30.0
        assert reconciler.rescue_timeout == 5.0


class TestCheckAndReconcileBalanced:
    """Tests for reconciling balanced positions."""

    def test_balanced_position_returns_merge_ready(self):
        """Should return MERGE_READY for balanced position."""
        reconciler = PositionReconciler()
        position = make_position(up_filled=50, down_filled=50, is_balanced=True)

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is True
        assert result.new_status == PositionStatus.MERGE_READY
        assert result.action_taken == "none"

    def test_closed_position_returns_closed(self):
        """Should return CLOSED for closed position."""
        reconciler = PositionReconciler()
        position = make_position()
        position.is_closed = True

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is True
        assert result.new_status == PositionStatus.CLOSED


class TestRescueWithTaker:
    """Tests for taker rescue strategy."""

    def test_rescue_needs_down_side(self):
        """Should buy DOWN when UP has more fills."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        reconciler = PositionReconciler(client=mock_client)
        position = make_position(up_filled=50, down_filled=40, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        # Should have called create_order for DOWN token
        mock_client.create_order.assert_called()
        call_kwargs = mock_client.create_order.call_args.kwargs
        assert call_kwargs["marketId"] == "token_down"
        assert call_kwargs["action"] == "BUY"
        assert call_kwargs["size"] == 10  # 50 - 40

    def test_rescue_needs_up_side(self):
        """Should buy UP when DOWN has more fills."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        reconciler = PositionReconciler(client=mock_client)
        position = make_position(up_filled=30, down_filled=50, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        # Should have called create_order for UP token
        call_kwargs = mock_client.create_order.call_args.kwargs
        assert call_kwargs["marketId"] == "token_up"
        assert call_kwargs["size"] == 20  # 50 - 30

    def test_rescue_success_updates_position(self):
        """Should update position fills on successful rescue."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        reconciler = PositionReconciler(client=mock_client)
        position = make_position(up_filled=50, down_filled=40, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is True
        assert position.down_filled == 50  # Was 40, now 50
        assert position.is_balanced is True
        assert reconciler.rescues_successful == 1

    def test_rescue_increments_attempted(self):
        """Should increment rescues_attempted counter."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        reconciler = PositionReconciler(client=mock_client)
        position = make_position(up_filled=50, down_filled=40, is_balanced=False)

        run_async(reconciler.check_and_reconcile(position))

        assert reconciler.rescues_attempted == 1

    def test_rescue_without_client_skips(self):
        """Should skip rescue when no client available."""
        reconciler = PositionReconciler(client=None)
        position = make_position(up_filled=50, down_filled=40, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is False
        assert result.action_taken == "rescue_skipped"

    def test_rescue_handles_order_failure(self):
        """Should handle failed rescue order."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {
            "success": False,
            "errorMsg": "Insufficient balance"
        }

        reconciler = PositionReconciler(client=mock_client)
        position = make_position(up_filled=50, down_filled=40, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is False
        assert "Insufficient balance" in result.details


class TestSevereImbalance:
    """Tests for severe imbalance handling."""

    def test_severe_imbalance_flagged(self):
        """Should flag severe imbalance when rescue fails."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"success": False, "errorMsg": "Failed"}

        reconciler = PositionReconciler(client=mock_client, max_imbalance_pct=10)
        # 50% imbalance: up=50, down=25
        position = make_position(up_filled=50, down_filled=25, target_size=50, is_balanced=False)

        result = run_async(reconciler.check_and_reconcile(position))

        assert result.success is False
        assert result.new_status == PositionStatus.FAILED
        assert reconciler.forced_exits == 1


class TestReconcileAll:
    """Tests for batch reconciliation."""

    def test_reconciles_multiple_positions(self):
        """Should reconcile all imbalanced positions."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {"orderID": "order123"}

        reconciler = PositionReconciler(client=mock_client)

        positions = [
            make_position(up_filled=50, down_filled=40, is_balanced=False),
            make_position(up_filled=30, down_filled=50, is_balanced=False),
        ]
        positions[0].id = "pos-1"
        positions[1].id = "pos-2"

        results = run_async(reconciler.reconcile_all(positions))

        assert len(results) == 2
        assert reconciler.rescues_attempted == 2

    def test_skips_balanced_positions(self):
        """Should skip already balanced positions."""
        reconciler = PositionReconciler()

        positions = [
            make_position(up_filled=50, down_filled=50, is_balanced=True),
            make_position(up_filled=50, down_filled=50, is_balanced=True),
        ]

        results = run_async(reconciler.reconcile_all(positions))

        assert len(results) == 0  # No reconciliation needed


class TestGetStatus:
    """Tests for status reporting."""

    def test_returns_complete_status(self):
        """Should return all status fields."""
        reconciler = PositionReconciler()
        reconciler.rescues_attempted = 10
        reconciler.rescues_successful = 8
        reconciler.forced_exits = 1

        status = reconciler.get_status()

        assert status["rescues_attempted"] == 10
        assert status["rescues_successful"] == 8
        assert status["forced_exits"] == 1
        assert status["rescue_success_rate"] == 80.0
