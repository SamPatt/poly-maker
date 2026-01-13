"""
Unit tests for InventoryManager.
"""
import pytest
from datetime import datetime

from rebates.active_quoting.inventory_manager import InventoryManager, InventoryLimits
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
    """Tests for liability calculation."""

    def test_liability_zero_for_no_position(self, manager):
        """No position should have zero liability."""
        liability = manager.calculate_liability("token1")
        assert liability == 0

    def test_liability_calculation(self, manager, buy_fill):
        """Liability should be size x entry price."""
        manager.update_from_fill(buy_fill)

        liability = manager.calculate_liability("token1")
        # 10 shares @ 0.50 = $5 max loss
        assert liability == pytest.approx(5.0)

    def test_total_liability_single_market(self, manager, buy_fill):
        """Total liability with single market."""
        manager.update_from_fill(buy_fill)

        total = manager.calculate_total_liability()
        assert total == pytest.approx(5.0)

    def test_total_liability_multiple_markets(self, manager):
        """Total liability across multiple markets."""
        fill1 = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,  # $5 liability
        )
        fill2 = Fill(
            order_id="order2",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.40,
            size=20.0,  # $8 liability
        )

        manager.update_from_fill(fill1)
        manager.update_from_fill(fill2)

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

    def test_summary_excludes_zero_positions(self, manager, buy_fill, sell_fill):
        """Should exclude zero positions from summary."""
        # Buy then sell all
        sell_fill_full = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            size=10.0,  # Sell all
        )
        manager.update_from_fill(buy_fill)
        manager.update_from_fill(sell_fill_full)

        summary = manager.get_summary()
        assert "token1" not in summary


class TestInventoryManagerRealizedPnL:
    """Tests for realized PnL tracking."""

    def test_realized_pnl_on_profitable_sale(self, manager, buy_fill, sell_fill):
        """Should track realized PnL on profitable sale."""
        manager.update_from_fill(buy_fill)  # Buy 10 @ 0.50
        manager.update_from_fill(sell_fill)  # Sell 5 @ 0.55

        pos = manager.get_position("token1")
        # PnL: (0.55 - 0.50) * 5 = $0.25
        assert pos.realized_pnl == pytest.approx(0.25)

    def test_realized_pnl_on_loss(self, manager):
        """Should track realized PnL on losing sale."""
        buy = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.60,
            size=10.0,
        )
        sell = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.50,
            size=10.0,
        )
        manager.update_from_fill(buy)
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
