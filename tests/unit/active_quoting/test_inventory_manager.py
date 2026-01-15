"""
Unit tests for InventoryManager.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from rebates.active_quoting.inventory_manager import (
    InventoryManager,
    InventoryLimits,
    TrackedPosition,
    PendingFill,
    PENDING_FILL_AGE_OUT_SECONDS,
)
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import Fill, OrderSide, Position


@pytest.fixture
def config():
    """Default configuration for tests."""
    return ActiveQuotingConfig(
        max_position_per_market=100,
        max_liability_per_market_usdc=50.0,
        max_total_liability_usdc=500.0,
        inventory_skew_coefficient=0.1,
    )


@pytest.fixture
def manager(config):
    """InventoryManager instance with default config."""
    return InventoryManager(config)


@pytest.fixture
def buy_fill():
    """Sample buy fill."""
    return Fill(
        order_id="order1",
        token_id="token1",
        side=OrderSide.BUY,
        price=0.50,
        size=10.0,
        fee=0.01,
    )


@pytest.fixture
def sell_fill():
    """Sample sell fill."""
    return Fill(
        order_id="order2",
        token_id="token1",
        side=OrderSide.SELL,
        price=0.55,
        size=5.0,
        fee=0.01,
    )


class TestInventoryManagerBasic:
    """Tests for basic position tracking."""

    def test_initial_position_is_zero(self, manager):
        """New tokens should have zero position."""
        pos = manager.get_position("token1")
        assert pos.size == 0
        assert pos.avg_entry_price == 0

    def test_get_inventory_zero_initially(self, manager):
        """Inventory should be zero initially."""
        assert manager.get_inventory("token1") == 0

    def test_update_from_buy_fill(self, manager, buy_fill):
        """Buy fill should increase position."""
        manager.update_from_fill(buy_fill)

        pos = manager.get_position("token1")
        assert pos.size == 10.0
        assert pos.avg_entry_price == 0.50

    def test_update_from_sell_fill(self, manager, buy_fill, sell_fill):
        """Sell fill should decrease position."""
        manager.update_from_fill(buy_fill)
        manager.update_from_fill(sell_fill)

        pos = manager.get_position("token1")
        assert pos.size == 5.0  # 10 - 5

    def test_multiple_buys_weighted_average(self, manager):
        """Multiple buys should calculate weighted average entry."""
        # First buy: 10 shares @ 0.50
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        # Second buy: 10 shares @ 0.60
        fill2 = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.60,
            size=10.0,
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        pos = manager.get_position("token1")
        assert pos.size == 20.0
        # Weighted avg: (10*0.50 + 10*0.60) / 20 = 0.55
        assert pos.avg_entry_price == pytest.approx(0.55)


class TestInventoryManagerLiability:
    """Tests for liability calculation (uses confirmed_size for limit checks)."""

    def test_liability_zero_for_no_position(self, manager):
        """No position should have zero liability."""
        liability = manager.calculate_liability("token1")
        assert liability == 0

    def test_liability_calculation(self, manager):
        """Liability should be confirmed_size x entry price."""
        # Use set_position to establish confirmed position
        manager.set_position("token1", size=10.0, avg_entry_price=0.50)

        liability = manager.calculate_liability("token1")
        # 10 shares @ 0.50 = $5 max loss
        assert liability == pytest.approx(5.0)

    def test_total_liability_single_market(self, manager):
        """Total liability with single market."""
        manager.set_position("token1", size=10.0, avg_entry_price=0.50)

        total = manager.calculate_total_liability()
        assert total == pytest.approx(5.0)

    def test_total_liability_multiple_markets(self, manager):
        """Total liability across multiple markets."""
        manager.set_position("token1", size=10.0, avg_entry_price=0.50)  # $5 liability
        manager.set_position("token2", size=20.0, avg_entry_price=0.40)  # $8 liability

        total = manager.calculate_total_liability()
        assert total == pytest.approx(13.0)


class TestInventoryManagerSkew:
    """Tests for skew factor calculation."""

    def test_skew_zero_for_no_position(self, manager):
        """No position should have zero skew."""
        skew = manager.calculate_skew_factor("token1")
        assert skew == 0

    def test_positive_inventory_positive_skew(self, manager, buy_fill):
        """Long position should have positive skew factor."""
        manager.update_from_fill(buy_fill)

        skew = manager.calculate_skew_factor("token1")
        # 10 shares * 0.1 coefficient = 1.0
        assert skew == pytest.approx(1.0)

    def test_skew_ticks_calculation(self, manager, buy_fill):
        """Skew in ticks should round correctly."""
        manager.update_from_fill(buy_fill)

        ticks = manager.calculate_skew_ticks("token1", tick_size=0.01)
        assert ticks == 1

    def test_skew_ticks_rounds_properly(self, manager):
        """Skew should round to nearest tick."""
        # 7 shares * 0.1 = 0.7, rounds to 1
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=7.0,
        )
        manager.update_from_fill(fill)

        ticks = manager.calculate_skew_ticks("token1", tick_size=0.01)
        assert ticks == 1

    def test_large_inventory_large_skew(self, manager):
        """Large inventory should produce larger skew."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=50.0,
        )
        manager.update_from_fill(fill)

        skew = manager.calculate_skew_factor("token1")
        # 50 * 0.1 = 5.0
        assert skew == pytest.approx(5.0)


class TestInventoryManagerLimits:
    """Tests for position limit enforcement."""

    def test_can_buy_and_sell_initially(self, manager):
        """Should allow both buy and sell initially (sell limited by no position)."""
        limits = manager.check_limits("token1")
        assert limits.can_buy is True
        assert limits.can_sell is False  # No position to sell
        assert "No position" in limits.sell_limit_reason

    def test_position_limit_blocks_buy(self, manager, config):
        """Should block buys at position limit."""
        # Set position at limit with low entry price so liability doesn't trigger first
        manager.set_position("token1", size=100, avg_entry_price=0.10)

        limits = manager.check_limits("token1")
        assert limits.can_buy is False
        assert "Position 100" in limits.buy_limit_reason
        assert limits.can_sell is True

    def test_liability_limit_blocks_buy(self, manager, config):
        """Should block buys at liability limit."""
        # Position with liability >= $50
        manager.set_position("token1", size=100, avg_entry_price=0.50)
        # 100 * 0.50 = $50 liability

        limits = manager.check_limits("token1")
        assert limits.can_buy is False
        assert "Liability" in limits.buy_limit_reason

    def test_total_liability_limit_blocks_buy(self, manager, config):
        """Should block buys at total liability limit."""
        # Create positions totaling >= $500
        for i in range(10):
            manager.set_position(f"token{i}", size=100, avg_entry_price=0.50)
            # Each: 100 * 0.50 = $50, total = $500

        limits = manager.check_limits("new_token")
        assert limits.can_buy is False
        assert "Total liability" in limits.buy_limit_reason

    def test_selling_always_allowed_with_position(self, manager):
        """Should always allow selling when holding position."""
        manager.set_position("token1", size=100, avg_entry_price=0.50)

        limits = manager.check_limits("token1")
        assert limits.can_sell is True


class TestInventoryManagerOrderPlacement:
    """Tests for order placement checks."""

    def test_can_place_buy_order(self, manager):
        """Should allow buy order within limits."""
        allowed, reason = manager.can_place_order("token1", OrderSide.BUY, 10)
        assert allowed is True
        assert reason == ""

    def test_buy_order_blocked_at_limit(self, manager):
        """Should block buy order at limit."""
        # Use low entry price so position limit triggers, not liability
        manager.set_position("token1", size=100, avg_entry_price=0.10)

        allowed, reason = manager.can_place_order("token1", OrderSide.BUY, 10)
        assert allowed is False
        assert "Position" in reason or "Liability" in reason

    def test_buy_order_blocked_if_would_exceed(self, manager):
        """Should block buy if it would exceed limit."""
        manager.set_position("token1", size=95, avg_entry_price=0.50)

        # 95 + 10 = 105 > 100 limit
        allowed, reason = manager.can_place_order("token1", OrderSide.BUY, 10)
        assert allowed is False
        assert "exceed" in reason.lower()

    def test_sell_order_blocked_without_position(self, manager):
        """Should block sell order without position."""
        allowed, reason = manager.can_place_order("token1", OrderSide.SELL, 10)
        assert allowed is False
        assert "No position" in reason

    def test_sell_order_allowed_with_position(self, manager):
        """Should allow sell order with position."""
        manager.set_position("token1", size=20, avg_entry_price=0.50)

        allowed, reason = manager.can_place_order("token1", OrderSide.SELL, 10)
        assert allowed is True


class TestInventoryManagerAdjustedSize:
    """Tests for adjusted order size calculation."""

    def test_full_size_within_limits(self, manager):
        """Should return full size when within limits."""
        size = manager.get_adjusted_order_size("token1", OrderSide.BUY, 10)
        assert size == 10

    def test_reduced_size_near_limit(self, manager):
        """Should reduce size when near limit."""
        manager.set_position("token1", size=95, avg_entry_price=0.50)

        # Only 5 shares of capacity remaining
        size = manager.get_adjusted_order_size("token1", OrderSide.BUY, 10)
        assert size == 5

    def test_zero_size_at_limit(self, manager):
        """Should return zero at limit."""
        manager.set_position("token1", size=100, avg_entry_price=0.50)

        size = manager.get_adjusted_order_size("token1", OrderSide.BUY, 10)
        assert size == 0

    def test_sell_size_capped_by_position(self, manager):
        """Should cap sell size to position."""
        manager.set_position("token1", size=5, avg_entry_price=0.50)

        size = manager.get_adjusted_order_size("token1", OrderSide.SELL, 10)
        assert size == 5

    def test_sell_zero_without_position(self, manager):
        """Should return zero sell size without position."""
        size = manager.get_adjusted_order_size("token1", OrderSide.SELL, 10)
        assert size == 0


class TestInventoryManagerReset:
    """Tests for reset functionality."""

    def test_reset_position(self, manager, buy_fill):
        """Should reset single position."""
        manager.update_from_fill(buy_fill)
        assert manager.get_inventory("token1") == 10

        manager.reset_position("token1")
        assert manager.get_inventory("token1") == 0

    def test_reset_all(self, manager):
        """Should reset all positions."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.40,
            size=20.0,
        )
        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        manager.reset_all()

        # Check positions dict is empty (don't call get_inventory as it re-creates)
        assert len(manager.positions) == 0


class TestInventoryManagerSetPosition:
    """Tests for direct position setting."""

    def test_set_position(self, manager):
        """Should set position directly."""
        manager.set_position("token1", size=50, avg_entry_price=0.45)

        pos = manager.get_position("token1")
        assert pos.size == 50
        assert pos.avg_entry_price == 0.45

    def test_set_position_overwrites(self, manager, buy_fill):
        """Should overwrite existing position."""
        manager.update_from_fill(buy_fill)
        manager.set_position("token1", size=100, avg_entry_price=0.60)

        pos = manager.get_position("token1")
        assert pos.size == 100
        assert pos.avg_entry_price == 0.60


class TestInventoryManagerSummary:
    """Tests for summary generation."""

    def test_summary_empty_initially(self, manager):
        """Should return empty summary initially."""
        summary = manager.get_summary()
        assert summary == {}

    def test_summary_includes_active_positions(self, manager, buy_fill):
        """Should include active positions in summary."""
        manager.update_from_fill(buy_fill)

        summary = manager.get_summary()
        assert "token1" in summary
        assert summary["token1"]["size"] == 10.0
        assert summary["token1"]["avg_entry"] == 0.50
        assert summary["token1"]["liability"] == pytest.approx(5.0)
        assert summary["token1"]["skew_factor"] == pytest.approx(1.0)

    def test_summary_excludes_zero_positions(self, manager):
        """Should exclude zero positions from summary after reconciliation."""
        # Set confirmed position, then API confirms it's zero (fully sold)
        manager.set_position("token1", size=10.0, avg_entry_price=0.50)
        manager.set_position("token1", size=0.0, avg_entry_price=0.0)

        summary = manager.get_summary()
        assert "token1" not in summary


class TestInventoryManagerRealizedPnL:
    """Tests for realized PnL tracking."""

    def test_realized_pnl_on_profitable_sale(self, manager):
        """Should track realized PnL on profitable sale against confirmed position."""
        # First, establish confirmed position via API sync
        manager.set_position("token1", size=10.0, avg_entry_price=0.50)

        # Then sell (via WebSocket fill)
        sell_fill = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            size=5.0,
        )
        manager.update_from_fill(sell_fill)

        pos = manager.get_position("token1")
        # PnL: (0.55 - 0.50) * 5 = $0.25
        assert pos.realized_pnl == pytest.approx(0.25)

    def test_realized_pnl_on_loss(self, manager):
        """Should track realized PnL on losing sale against confirmed position."""
        # First, establish confirmed position via API sync
        manager.set_position("token1", size=10.0, avg_entry_price=0.60)

        # Then sell at a loss (via WebSocket fill)
        sell = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.50,
            size=10.0,
        )
        manager.update_from_fill(sell)

        pos = manager.get_position("token1")
        # PnL: (0.50 - 0.60) * 10 = -$1.00
        assert pos.realized_pnl == pytest.approx(-1.0)


class TestInventoryManagerFees:
    """Tests for fee tracking."""

    def test_fees_tracked_from_fills(self, manager, buy_fill):
        """Should track total fees paid."""
        manager.update_from_fill(buy_fill)

        pos = manager.get_position("token1")
        assert pos.total_fees_paid == pytest.approx(0.01)

    def test_fees_accumulate(self, manager, buy_fill, sell_fill):
        """Should accumulate fees across fills."""
        manager.update_from_fill(buy_fill)
        manager.update_from_fill(sell_fill)

        pos = manager.get_position("token1")
        assert pos.total_fees_paid == pytest.approx(0.02)


class TestInventoryManagerEdgeCases:
    """Tests for edge cases."""

    def test_very_small_fill(self, manager):
        """Should handle very small fills."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=0.001,
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert pos.size == pytest.approx(0.001)

    def test_high_price_fill(self, manager):
        """Should handle high price fills."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.99,
            size=10.0,
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        # Liability: 10 * 0.99 = $9.90
        assert pos.max_liability == pytest.approx(9.9)

    def test_low_price_fill(self, manager):
        """Should handle low price fills."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.01,
            size=100.0,
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        # Liability: 100 * 0.01 = $1.00
        assert pos.max_liability == pytest.approx(1.0)

    def test_multiple_tokens_independent(self, manager):
        """Different tokens should be tracked independently."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.40,
            size=20.0,
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        assert manager.get_inventory("token1") == 10.0
        assert manager.get_inventory("token2") == 20.0
        assert manager.calculate_skew_factor("token1") == pytest.approx(1.0)
        assert manager.calculate_skew_factor("token2") == pytest.approx(2.0)


# =============================================================================
# Phase 2: Dual Tracking Tests
# =============================================================================


class TestTrackedPositionDataclass:
    """Tests for TrackedPosition dataclass properties."""

    def test_effective_size_with_no_pending(self):
        """Effective size should equal confirmed when no pending fills."""
        pos = TrackedPosition(token_id="token1", confirmed_size=50.0)
        assert pos.effective_size == 50.0
        assert pos.size == 50.0  # Alias should work

    def test_effective_size_with_pending_buys(self):
        """Effective size should include pending buy fills."""
        pos = TrackedPosition(token_id="token1", confirmed_size=50.0)
        pos.pending_fills["t1"] = PendingFill(
            trade_id="t1",
            side=OrderSide.BUY,
            size=10.0,
            price=0.50,
            timestamp=datetime.utcnow(),
        )
        assert pos.effective_size == 60.0
        assert pos.pending_fill_buys == 10.0
        assert pos.pending_fill_sells == 0.0

    def test_effective_size_with_pending_sells(self):
        """Effective size should subtract pending sell fills."""
        pos = TrackedPosition(token_id="token1", confirmed_size=50.0)
        pos.pending_fills["t1"] = PendingFill(
            trade_id="t1",
            side=OrderSide.SELL,
            size=10.0,
            price=0.55,
            timestamp=datetime.utcnow(),
        )
        assert pos.effective_size == 40.0
        assert pos.pending_fill_buys == 0.0
        assert pos.pending_fill_sells == 10.0

    def test_effective_size_mixed_pending(self):
        """Effective size should handle mixed buy/sell pending fills."""
        pos = TrackedPosition(token_id="token1", confirmed_size=50.0)
        pos.pending_fills["t1"] = PendingFill(
            trade_id="t1",
            side=OrderSide.BUY,
            size=20.0,
            price=0.50,
            timestamp=datetime.utcnow(),
        )
        pos.pending_fills["t2"] = PendingFill(
            trade_id="t2",
            side=OrderSide.SELL,
            size=5.0,
            price=0.55,
            timestamp=datetime.utcnow(),
        )
        # 50 + 20 - 5 = 65
        assert pos.effective_size == 65.0
        assert pos.pending_delta == 15.0

    def test_pending_fill_delta(self):
        """PendingFill delta should be positive for buys, negative for sells."""
        buy_fill = PendingFill(
            trade_id="t1",
            side=OrderSide.BUY,
            size=10.0,
            price=0.50,
            timestamp=datetime.utcnow(),
        )
        sell_fill = PendingFill(
            trade_id="t2",
            side=OrderSide.SELL,
            size=5.0,
            price=0.55,
            timestamp=datetime.utcnow(),
        )
        assert buy_fill.delta == 10.0
        assert sell_fill.delta == -5.0


class TestDualTrackingFillHandling:
    """Tests for Phase 2 fill handling (pending fills)."""

    def test_fill_adds_to_pending_not_confirmed(self, manager, buy_fill):
        """WebSocket fill should add to pending, not confirmed."""
        manager.update_from_fill(buy_fill)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 0.0  # Not updated
        assert len(pos.pending_fills) == 1  # Has pending
        assert pos.effective_size == 10.0  # Combined is correct

    def test_multiple_fills_accumulate_in_pending(self, manager):
        """Multiple fills should accumulate in pending_fills."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="trade1",
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.52,
            size=20.0,
            trade_id="trade2",
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 2
        assert pos.effective_size == 30.0


class TestMissingTradeIdSynthesis:
    """Tests for synthesized trade_id when missing (Phase 2 requirement)."""

    def test_missing_trade_id_uses_synthesized_key(self, manager):
        """Fill without trade_id should get a synthesized unique key."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id=None,  # Missing!
        )

        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 1

        # Get the synthesized key
        trade_id = list(pos.pending_fills.keys())[0]
        assert trade_id.startswith("order1_")  # Contains order_id
        assert "10.00" in trade_id  # Contains size

    def test_multiple_fills_without_trade_id_dont_collide(self, manager):
        """Multiple fills without trade_id should get unique keys."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id=None,
            timestamp=datetime.utcnow(),
        )
        fill2 = Fill(
            order_id="order1",  # Same order, different fill
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=5.0,  # Different size
            trade_id=None,
            timestamp=datetime.utcnow() + timedelta(milliseconds=1),
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        pos = manager.get_position("token1")
        # Both fills should be present (unique keys)
        assert len(pos.pending_fills) == 2
        assert pos.effective_size == 15.0


class TestPartialConfirmationReconciliation:
    """Tests for partial confirmation reconciliation (Phase 2 requirement)."""

    def test_partial_confirmation_removes_oldest_fills_first(self, manager):
        """API absorbs 50 of 80 pending, verify oldest fills removed, newest kept."""
        # Create 3 fills with different timestamps
        now = datetime.utcnow()
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=30.0,
            trade_id="oldest",
            timestamp=now - timedelta(seconds=10),
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=20.0,
            trade_id="middle",
            timestamp=now - timedelta(seconds=5),
        )
        fill3 = Fill(
            order_id="order3",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=30.0,
            trade_id="newest",
            timestamp=now,
        )

        # Add all fills to pending
        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)
        manager.update_from_fill(fill3)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 3
        assert pos.effective_size == 80.0

        # API sync: confirmed moves from 0 to 50 (absorbs 50)
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        # Should have absorbed oldest (30) + middle (20) = 50
        # Only newest (30) should remain
        assert pos.confirmed_size == 50.0
        assert len(pos.pending_fills) == 1
        assert "newest" in pos.pending_fills
        assert pos.effective_size == 80.0  # 50 confirmed + 30 pending

    def test_reconciliation_handles_sell_fills(self, manager):
        """Reconciliation should handle sell fills correctly."""
        # Start with confirmed position
        manager.set_position("token1", size=100.0, avg_entry_price=0.50)

        # Add pending sell
        sell_fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            size=20.0,
            trade_id="sell1",
        )
        manager.update_from_fill(sell_fill)

        pos = manager.get_position("token1")
        assert pos.effective_size == 80.0  # 100 - 20

        # API sync: confirmed drops to 80 (absorbed the sell)
        manager.set_position("token1", size=80.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 80.0
        assert len(pos.pending_fills) == 0  # Sell absorbed
        assert pos.effective_size == 80.0

    def test_partial_absorption_reduces_fill_size(self, manager):
        """API partially absorbing a large fill should reduce its size, not keep it whole."""
        # Pending BUY of 100
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=100.0,
            trade_id="big_fill",
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 0.0
        assert pos.effective_size == 100.0

        # API moves +50 (partial absorption)
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 50.0
        # Pending fill should be reduced from 100 to 50
        assert len(pos.pending_fills) == 1
        assert pos.pending_fills["big_fill"].size == 50.0
        assert pos.effective_size == 100.0  # 50 confirmed + 50 pending

        # API moves another +50 (completes absorption)
        manager.set_position("token1", size=100.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 100.0
        # Remaining 50 should now be fully absorbed
        assert len(pos.pending_fills) == 0
        assert pos.effective_size == 100.0


class TestAgeOutPendingFills:
    """Tests for age-out of old pending fills (Phase 2 requirement)."""

    def test_age_out_logs_trade_ids_and_delta(self, manager, caplog):
        """Fills older than 30s should be removed with proper logging."""
        import logging
        caplog.set_level(logging.WARNING)

        # Create an old fill (older than age-out threshold)
        old_timestamp = datetime.utcnow() - timedelta(seconds=PENDING_FILL_AGE_OUT_SECONDS + 5)
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=25.0,
            trade_id="old_fill",
            timestamp=old_timestamp,
        )

        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 1

        # Trigger age-out via set_position (which calls _age_out_pending_fills)
        manager.set_position("token1", size=0.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 0

        # Check logging
        assert "Aging out pending fills" in caplog.text
        assert "old_fill" in caplog.text
        assert "+25.00" in caplog.text or "25.00" in caplog.text

    def test_recent_fills_not_aged_out(self, manager):
        """Fills newer than threshold should not be aged out."""
        # Recent fill
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="recent",
            timestamp=datetime.utcnow(),
        )

        manager.update_from_fill(fill)

        # Trigger age-out check
        manager.set_position("token1", size=0.0, avg_entry_price=0.50)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 1  # Still there
        assert "recent" in pos.pending_fills


class TestForceReconciliation:
    """Tests for force reconciliation on WS reconnect/gaps."""

    def test_force_reconcile_clears_pending_fills(self, manager):
        """Force reconcile should clear all pending fills."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 1

        manager.force_reconcile("token1")

        pos = manager.get_position("token1")
        assert len(pos.pending_fills) == 0

    def test_force_reconcile_all(self, manager):
        """Force reconcile all should clear pending for all tokens."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.40,
            size=20.0,
            trade_id="fill2",
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

        manager.force_reconcile_all()

        assert len(manager.get_position("token1").pending_fills) == 0
        assert len(manager.get_position("token2").pending_fills) == 0

    def test_force_reconcile_logs_discrepancy(self, manager, caplog):
        """Force reconcile should log discrepancy before clearing."""
        import logging
        caplog.set_level(logging.WARNING)

        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        manager.force_reconcile("token1")

        assert "Force reconcile" in caplog.text
        assert "fill1" in caplog.text


class TestEffectiveSizeUsedByConsumers:
    """Tests to verify effective_size and confirmed_size are used appropriately.

    Phase 2 behavior:
    - Buy limits use confirmed_size (conservative)
    - Sell availability uses effective_size (allows quick exits after WS fills)
    """

    def test_get_inventory_returns_effective_size(self, manager):
        """get_inventory should return effective_size not confirmed_size."""
        # Set confirmed position
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        # Add pending buy
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        # get_inventory should return effective (60), not confirmed (50)
        assert manager.get_inventory("token1") == 60.0

    def test_check_limits_buys_use_confirmed_size(self, manager):
        """Buy limits should use confirmed_size (Phase 2 behavior)."""
        # Set confirmed at 90 (below 100 limit)
        manager.set_position("token1", size=90.0, avg_entry_price=0.10)

        # Add pending buy of 15 (effective = 105, but buy limits use confirmed = 90)
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.10,
            size=15.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 90.0
        assert pos.effective_size == 105.0

        # Should still allow buys because confirmed_size (90) < limit (100)
        # Phase 3 will add conservative exposure formula
        limits = manager.check_limits("token1")
        assert limits.can_buy is True  # Pending fills don't affect buy limit checks yet

    def test_check_limits_sells_use_effective_size(self, manager):
        """Sell availability should use effective_size (allows quick exits after WS fills)."""
        # No confirmed position, but add a pending buy fill
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=20.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        pos = manager.get_position("token1")
        assert pos.confirmed_size == 0.0  # API hasn't confirmed yet
        assert pos.effective_size == 20.0  # But we have pending buy

        # Should allow sells because effective_size > 0
        # This enables quick exits after WS fills before API sync
        limits = manager.check_limits("token1")
        assert limits.can_sell is True
        assert limits.sell_limit_reason == ""

    def test_adjusted_sell_size_uses_effective(self, manager):
        """get_adjusted_order_size for sells should use effective_size."""
        # Only pending fills, no confirmed position
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=15.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        # Should be able to sell up to effective_size (15)
        size = manager.get_adjusted_order_size("token1", OrderSide.SELL, 20.0)
        assert size == 15.0  # Capped to effective_size

    def test_check_limits_blocks_at_confirmed_limit(self, manager):
        """check_limits should block when confirmed_size >= limit."""
        # Set confirmed at limit
        manager.set_position("token1", size=100.0, avg_entry_price=0.10)

        limits = manager.check_limits("token1")
        assert limits.can_buy is False
        assert "Position 100" in limits.buy_limit_reason

    def test_liability_uses_confirmed_size_for_limits(self, manager):
        """Liability calculation uses confirmed_size for limit checks (Phase 2)."""
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        # calculate_liability uses confirmed_size for limit checks
        # 50 * 0.50 = 25.0
        liability = manager.calculate_liability("token1")
        assert liability == pytest.approx(25.0)

        # But TrackedPosition.max_liability uses effective_size
        pos = manager.get_position("token1")
        assert pos.max_liability == pytest.approx(30.0)  # 60 * 0.50

    def test_skew_uses_effective_size(self, manager):
        """Skew calculation should use effective_size."""
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        # Skew should use effective_size (60)
        # 60 * 0.1 coefficient = 6.0
        skew = manager.calculate_skew_factor("token1")
        assert skew == pytest.approx(6.0)

    def test_summary_includes_both_confirmed_and_effective(self, manager):
        """get_summary should include both confirmed and effective sizes."""
        manager.set_position("token1", size=50.0, avg_entry_price=0.50)

        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="fill1",
        )
        manager.update_from_fill(fill)

        summary = manager.get_summary()
        assert "token1" in summary
        assert summary["token1"]["size"] == 60.0  # effective
        assert summary["token1"]["confirmed_size"] == 50.0
        assert summary["token1"]["pending_delta"] == 10.0
        assert summary["token1"]["pending_fills_count"] == 1
