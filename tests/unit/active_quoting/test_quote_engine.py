"""
Unit tests for QuoteEngine.
"""
import pytest
from datetime import datetime, timedelta

from rebates.active_quoting.quote_engine import (
    QuoteEngine,
    QuoteAction,
    QuoteDecision,
)
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import (
    OrderbookState,
    OrderbookLevel,
    Quote,
    MomentumState,
    OrderSide,
)


@pytest.fixture
def config():
    """Default configuration for tests."""
    return ActiveQuotingConfig(
        quote_offset_ticks=0,
        improve_when_spread_ticks=4,
        refresh_threshold_ticks=2,
        inventory_skew_coefficient=0.1,
        order_size_usdc=10.0,
    )


@pytest.fixture
def engine(config):
    """QuoteEngine instance with default config."""
    return QuoteEngine(config)


@pytest.fixture
def basic_orderbook():
    """Orderbook with 2-tick spread (no improvement)."""
    return OrderbookState(
        token_id="token1",
        bids=[
            OrderbookLevel(price=0.49, size=100.0),
            OrderbookLevel(price=0.48, size=200.0),
        ],
        asks=[
            OrderbookLevel(price=0.51, size=100.0),
            OrderbookLevel(price=0.52, size=200.0),
        ],
        tick_size=0.01,
    )


@pytest.fixture
def wide_spread_orderbook():
    """Orderbook with 6-tick spread (improvement triggered)."""
    return OrderbookState(
        token_id="token1",
        bids=[
            OrderbookLevel(price=0.47, size=100.0),
        ],
        asks=[
            OrderbookLevel(price=0.53, size=100.0),
        ],
        tick_size=0.01,
    )


class TestQuoteEngineBasicQuoting:
    """Tests for basic quote calculation."""

    def test_quote_at_best_bid_ask_narrow_spread(self, engine, basic_orderbook):
        """Should quote AT best bid/ask when spread is narrow."""
        decision = engine.calculate_quote(basic_orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote is not None
        assert decision.quote.bid_price == 0.49  # AT best bid
        assert decision.quote.ask_price == 0.51  # AT best ask

    def test_improve_when_spread_wide(self, engine, wide_spread_orderbook):
        """Should improve by 1 tick when spread >= improve_when_spread_ticks."""
        decision = engine.calculate_quote(wide_spread_orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote is not None
        # Spread is 6 ticks (0.47 to 0.53), >= 4 ticks threshold
        assert decision.quote.bid_price == 0.48  # best_bid + 1 tick
        assert decision.quote.ask_price == 0.52  # best_ask - 1 tick

    def test_no_improve_when_spread_below_threshold(self, engine):
        """Should not improve when spread < improve_when_spread_ticks."""
        # 3-tick spread
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.48, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )
        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote.bid_price == 0.48  # AT best bid
        assert decision.quote.ask_price == 0.51  # AT best ask

    def test_exactly_at_threshold(self, engine):
        """Should improve when spread == improve_when_spread_ticks (4 ticks)."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.48, size=100.0)],
            asks=[OrderbookLevel(price=0.52, size=100.0)],
            tick_size=0.01,
        )
        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # Spread is exactly 4 ticks, should improve
        assert decision.quote.bid_price == 0.49
        assert decision.quote.ask_price == 0.51


class TestQuoteEngineInventorySkew:
    """Tests for inventory skewing logic."""

    def test_positive_inventory_skew(self, engine, basic_orderbook):
        """Positive inventory (long) should lower both bid and ask."""
        # 10 shares inventory * 0.1 coefficient = 1 tick skew
        decision = engine.calculate_quote(basic_orderbook, inventory=10.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # Base: bid=0.49, ask=0.51
        # Skew: -1 tick on both
        assert decision.quote.bid_price == 0.48  # Lower bid (less aggressive buying)
        assert decision.quote.ask_price == 0.50  # Lower ask (more aggressive selling)

    def test_negative_inventory_skew(self, engine, basic_orderbook):
        """Negative inventory (short) should raise both bid and ask."""
        # -10 shares inventory * 0.1 coefficient = -1 tick skew (raises prices)
        decision = engine.calculate_quote(basic_orderbook, inventory=-10.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # Base: bid=0.49, ask=0.51
        # Skew: +1 tick on both
        assert decision.quote.bid_price == 0.50  # Higher bid (more aggressive buying)
        assert decision.quote.ask_price == 0.52  # Higher ask (less aggressive selling)

    def test_zero_inventory_no_skew(self, engine, basic_orderbook):
        """Zero inventory should not skew quotes."""
        decision = engine.calculate_quote(basic_orderbook, inventory=0.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote.bid_price == 0.49
        assert decision.quote.ask_price == 0.51

    def test_large_inventory_clamped(self, engine, basic_orderbook):
        """Large inventory skew should be clamped to prevent crossing."""
        # 100 shares * 0.1 = 10 tick skew - would cross if not clamped
        decision = engine.calculate_quote(basic_orderbook, inventory=100.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # Should be clamped to not cross
        assert decision.quote.bid_price < decision.quote.ask_price

    def test_zero_coefficient_no_skew(self):
        """Zero skew coefficient should not skew regardless of inventory."""
        config = ActiveQuotingConfig(
            inventory_skew_coefficient=0.0,
        )
        engine = QuoteEngine(config)
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook, inventory=50.0)

        assert decision.quote.bid_price == 0.49
        assert decision.quote.ask_price == 0.51


class TestQuoteEngineHysteresis:
    """Tests for hysteresis (refresh threshold) logic."""

    def test_keep_current_within_threshold(self, engine, basic_orderbook):
        """Should keep current quote if within hysteresis threshold."""
        current_quote = Quote(
            token_id="token1",
            bid_price=0.48,  # 1 tick from target (0.49)
            ask_price=0.51,  # At target
            bid_size=10.0,
            ask_size=10.0,
        )

        decision = engine.calculate_quote(basic_orderbook, current_quote=current_quote)

        # 1 tick difference < 2 tick threshold
        assert decision.action == QuoteAction.KEEP_CURRENT
        assert "hysteresis" in decision.reason.lower()

    def test_refresh_when_beyond_threshold(self, engine, basic_orderbook):
        """Should refresh quote if beyond hysteresis threshold."""
        current_quote = Quote(
            token_id="token1",
            bid_price=0.46,  # 3 ticks from target (0.49)
            ask_price=0.51,  # At target
            bid_size=10.0,
            ask_size=10.0,
        )

        decision = engine.calculate_quote(basic_orderbook, current_quote=current_quote)

        # 3 tick difference >= 2 tick threshold
        assert decision.action == QuoteAction.PLACE_QUOTE

    def test_refresh_when_ask_beyond_threshold(self, engine, basic_orderbook):
        """Should refresh if only ask is beyond threshold."""
        current_quote = Quote(
            token_id="token1",
            bid_price=0.49,  # At target
            ask_price=0.54,  # 3 ticks from target (0.51)
            bid_size=10.0,
            ask_size=10.0,
        )

        decision = engine.calculate_quote(basic_orderbook, current_quote=current_quote)

        assert decision.action == QuoteAction.PLACE_QUOTE

    def test_exactly_at_threshold(self, engine, basic_orderbook):
        """Should refresh when exactly at threshold."""
        current_quote = Quote(
            token_id="token1",
            bid_price=0.47,  # 2 ticks from target (0.49) - exactly at threshold
            ask_price=0.51,
            bid_size=10.0,
            ask_size=10.0,
        )

        decision = engine.calculate_quote(basic_orderbook, current_quote=current_quote)

        # Exactly at threshold should trigger refresh
        assert decision.action == QuoteAction.PLACE_QUOTE


class TestQuoteEngineMomentum:
    """Tests for momentum cooldown handling."""

    def test_cancel_during_cooldown(self, engine, basic_orderbook):
        """Should cancel all quotes during momentum cooldown."""
        momentum = MomentumState(
            token_id="token1",
            is_active=True,
            cooldown_until=datetime.utcnow() + timedelta(seconds=5),
        )

        decision = engine.calculate_quote(basic_orderbook, momentum_state=momentum)

        assert decision.action == QuoteAction.CANCEL_ALL
        assert "cooldown" in decision.reason.lower()

    def test_quote_after_cooldown_expired(self, engine, basic_orderbook):
        """Should quote normally after cooldown expires."""
        momentum = MomentumState(
            token_id="token1",
            is_active=True,
            cooldown_until=datetime.utcnow() - timedelta(seconds=5),  # Expired
        )

        decision = engine.calculate_quote(basic_orderbook, momentum_state=momentum)

        assert decision.action == QuoteAction.PLACE_QUOTE

    def test_quote_when_momentum_not_active(self, engine, basic_orderbook):
        """Should quote normally when momentum is not active."""
        momentum = MomentumState(
            token_id="token1",
            is_active=False,
        )

        decision = engine.calculate_quote(basic_orderbook, momentum_state=momentum)

        assert decision.action == QuoteAction.PLACE_QUOTE


class TestQuoteEngineInvalidOrderbook:
    """Tests for invalid orderbook handling."""

    def test_cancel_on_empty_orderbook(self, engine):
        """Should cancel all when orderbook is empty."""
        orderbook = OrderbookState(token_id="token1")

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.CANCEL_ALL
        assert "invalid" in decision.reason.lower() or "empty" in decision.reason.lower()

    def test_cancel_on_crossed_orderbook(self, engine):
        """Should cancel all when orderbook is crossed."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.52, size=100.0)],
            asks=[OrderbookLevel(price=0.48, size=100.0)],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.CANCEL_ALL

    def test_cancel_on_no_bids(self, engine):
        """Should cancel when no bids."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.CANCEL_ALL

    def test_cancel_on_no_asks(self, engine):
        """Should cancel when no asks."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.CANCEL_ALL


class TestQuoteEnginePriceClamping:
    """Tests for price clamping logic."""

    def test_clamp_bid_at_minimum(self, engine):
        """Bid should not go below tick_size."""
        # Very low prices
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.02, size=100.0)],
            asks=[OrderbookLevel(price=0.05, size=100.0)],
            tick_size=0.01,
        )

        # Large negative inventory would push bid below 0
        decision = engine.calculate_quote(orderbook, inventory=-100.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote.bid_price >= 0.01  # At least one tick

    def test_clamp_ask_at_maximum(self, engine):
        """Ask should not exceed 1.0 - tick_size."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.95, size=100.0)],
            asks=[OrderbookLevel(price=0.98, size=100.0)],
            tick_size=0.01,
        )

        # Positive inventory would push ask higher
        decision = engine.calculate_quote(orderbook, inventory=100.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote.ask_price <= 0.99  # At most 1.0 - tick

    def test_clamp_prevents_crossing_from_improvement(self, engine):
        """Improvement should not cause crossing."""
        # 4-tick spread exactly, improvement would bring them to same price
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.53, size=100.0)],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # After improvement: bid=0.50, ask=0.52 - valid
        assert decision.quote.bid_price < decision.quote.ask_price


class TestQuoteEngineSingleSide:
    """Tests for single-side quote calculation."""

    def test_calculate_bid_side(self, engine, basic_orderbook):
        """Should calculate bid price correctly."""
        price, reason = engine.calculate_quote_for_side(
            basic_orderbook, OrderSide.BUY
        )

        assert price == 0.49
        assert reason is not None

    def test_calculate_ask_side(self, engine, basic_orderbook):
        """Should calculate ask price correctly."""
        price, reason = engine.calculate_quote_for_side(
            basic_orderbook, OrderSide.SELL
        )

        assert price == 0.51
        assert reason is not None

    def test_single_side_with_inventory(self, engine, basic_orderbook):
        """Single side should apply inventory skew."""
        price, _ = engine.calculate_quote_for_side(
            basic_orderbook, OrderSide.BUY, inventory=10.0
        )

        # 10 * 0.1 = 1 tick lower
        assert price == 0.48

    def test_single_side_momentum_cooldown(self, engine, basic_orderbook):
        """Single side should respect momentum cooldown."""
        momentum = MomentumState(
            token_id="token1",
            is_active=True,
            cooldown_until=datetime.utcnow() + timedelta(seconds=5),
        )

        price, reason = engine.calculate_quote_for_side(
            basic_orderbook, OrderSide.BUY, momentum_state=momentum
        )

        assert price is None
        assert "cooldown" in reason.lower()


class TestQuoteEngineUtilities:
    """Tests for utility methods."""

    def test_get_spread_ticks(self, engine, basic_orderbook):
        """Should return correct spread in ticks."""
        spread_ticks = engine.get_spread_ticks(basic_orderbook)
        assert spread_ticks == 2

    def test_get_spread_ticks_empty(self, engine):
        """Should return None for empty orderbook."""
        orderbook = OrderbookState(token_id="token1")
        spread_ticks = engine.get_spread_ticks(orderbook)
        assert spread_ticks is None

    def test_is_spread_wide_enough_true(self, engine, wide_spread_orderbook):
        """Should return True for wide spread."""
        assert engine.is_spread_wide_enough(wide_spread_orderbook) is True

    def test_is_spread_wide_enough_false(self, engine, basic_orderbook):
        """Should return False for narrow spread."""
        assert engine.is_spread_wide_enough(basic_orderbook) is False

    def test_is_spread_wide_enough_empty(self, engine):
        """Should return False for empty orderbook."""
        orderbook = OrderbookState(token_id="token1")
        assert engine.is_spread_wide_enough(orderbook) is False


class TestQuoteEngineOrderSize:
    """Tests for order size from config."""

    def test_uses_config_order_size(self, config, basic_orderbook):
        """Quote should use order_size_usdc from config."""
        config.order_size_usdc = 25.0
        engine = QuoteEngine(config)

        decision = engine.calculate_quote(basic_orderbook)

        assert decision.quote.bid_size == 25.0
        assert decision.quote.ask_size == 25.0


class TestQuoteEngineEdgeCases:
    """Tests for edge cases."""

    def test_very_small_tick_size(self):
        """Should handle very small tick sizes (0.001)."""
        config = ActiveQuotingConfig(
            improve_when_spread_ticks=4,
            refresh_threshold_ticks=2,
        )
        engine = QuoteEngine(config)

        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.499, size=100.0)],
            asks=[OrderbookLevel(price=0.501, size=100.0)],
            tick_size=0.001,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # 2 tick spread < 4, so no improvement
        assert decision.quote.bid_price == 0.499
        assert decision.quote.ask_price == 0.501

    def test_different_tick_sizes(self):
        """Should work with different tick sizes."""
        config = ActiveQuotingConfig(improve_when_spread_ticks=4)
        engine = QuoteEngine(config)

        # 8 tick spread at 0.01 tick size = should improve
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.46, size=100.0)],
            asks=[OrderbookLevel(price=0.54, size=100.0)],
            tick_size=0.01,
        )

        decision = engine.calculate_quote(orderbook)

        assert decision.quote.bid_price == pytest.approx(0.47)  # improved by 1 tick
        assert decision.quote.ask_price == pytest.approx(0.53)  # improved by 1 tick

    def test_inventory_skew_rounds_to_ticks(self, engine):
        """Inventory skew should round to whole ticks."""
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        # 7 * 0.1 = 0.7, rounds to 1 tick
        decision = engine.calculate_quote(orderbook, inventory=7.0)

        assert decision.quote.bid_price == 0.48  # 1 tick down
        assert decision.quote.ask_price == 0.50  # 1 tick down

    def test_quote_token_id_preserved(self, engine, basic_orderbook):
        """Quote should preserve token_id from orderbook."""
        decision = engine.calculate_quote(basic_orderbook)

        assert decision.quote.token_id == "token1"


class TestQuoteEngineInventoryManagerIntegration:
    """Tests for InventoryManager integration."""

    @pytest.fixture
    def inventory_manager(self, config):
        """InventoryManager for testing."""
        from rebates.active_quoting.inventory_manager import InventoryManager
        return InventoryManager(config)

    @pytest.fixture
    def engine_with_manager(self, config, inventory_manager):
        """QuoteEngine with InventoryManager."""
        return QuoteEngine(config, inventory_manager=inventory_manager)

    def test_set_inventory_manager(self, engine, inventory_manager):
        """Should set inventory manager."""
        assert engine.inventory_manager is None
        engine.set_inventory_manager(inventory_manager)
        assert engine.inventory_manager is inventory_manager

    def test_constructor_with_manager(self, config, inventory_manager):
        """Should accept inventory manager in constructor."""
        engine = QuoteEngine(config, inventory_manager=inventory_manager)
        assert engine.inventory_manager is inventory_manager

    def test_calculate_quote_with_manager(self, engine_with_manager, basic_orderbook, inventory_manager):
        """Should calculate quote using inventory from manager."""
        # Set up some inventory
        from rebates.active_quoting.models import Fill, OrderSide
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        inventory_manager.update_from_fill(fill)

        decision = engine_with_manager.calculate_quote_with_manager(basic_orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # With 10 shares inventory and 0.1 coefficient = 1 tick skew
        assert decision.quote.bid_price == 0.48  # Skewed down
        assert decision.quote.ask_price == 0.50  # Skewed down

    def test_calculate_quote_with_manager_no_inventory(self, engine_with_manager, basic_orderbook):
        """Should work with zero inventory."""
        decision = engine_with_manager.calculate_quote_with_manager(basic_orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # No skew
        assert decision.quote.bid_price == 0.49
        assert decision.quote.ask_price == 0.51

    def test_calculate_quote_with_manager_raises_without_manager(self, engine, basic_orderbook):
        """Should raise if no manager set."""
        with pytest.raises(ValueError, match="InventoryManager not set"):
            engine.calculate_quote_with_manager(basic_orderbook)

    def test_calculate_quote_for_side_with_manager(self, engine_with_manager, basic_orderbook, inventory_manager):
        """Should calculate single side with manager."""
        from rebates.active_quoting.models import Fill, OrderSide
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        inventory_manager.update_from_fill(fill)

        price, reason = engine_with_manager.calculate_quote_for_side_with_manager(
            basic_orderbook, OrderSide.SELL
        )

        assert price == 0.50  # Skewed down
        assert reason is not None

    def test_calculate_quote_for_side_respects_limits(self, engine_with_manager, basic_orderbook, inventory_manager):
        """Should respect position limits from manager."""
        # Set position at limit
        inventory_manager.set_position("token1", size=100, avg_entry_price=0.10)

        price, reason = engine_with_manager.calculate_quote_for_side_with_manager(
            basic_orderbook, OrderSide.BUY
        )

        assert price is None
        assert "Position" in reason

    def test_calculate_quote_for_side_sell_blocked_no_position(self, engine_with_manager, basic_orderbook):
        """Should block sell when no position."""
        price, reason = engine_with_manager.calculate_quote_for_side_with_manager(
            basic_orderbook, OrderSide.SELL
        )

        assert price is None
        assert "No position" in reason

    def test_get_inventory_adjusted_sizes(self, engine_with_manager, inventory_manager):
        """Should get adjusted sizes from manager."""
        # Set some position
        inventory_manager.set_position("token1", size=50, avg_entry_price=0.50)

        buy_size, sell_size = engine_with_manager.get_inventory_adjusted_sizes("token1", 100.0)

        # Can buy 50 more (100 - 50)
        assert buy_size == 50
        # Can sell up to position
        assert sell_size == 50

    def test_get_inventory_adjusted_sizes_no_position(self, engine_with_manager):
        """Should return full buy, zero sell without position."""
        buy_size, sell_size = engine_with_manager.get_inventory_adjusted_sizes("token1", 50.0)

        assert buy_size == 50.0
        assert sell_size == 0.0

    def test_get_inventory_adjusted_sizes_raises_without_manager(self, engine):
        """Should raise if no manager set."""
        with pytest.raises(ValueError, match="InventoryManager not set"):
            engine.get_inventory_adjusted_sizes("token1", 10.0)
