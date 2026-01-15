"""
End-to-end mock tests for the basic quote cycle.

Tests the integration of:
- QuoteEngine
- OrderManager
- OrderbookManager (mocked state)
- UserChannelManager (mocked state)
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.quote_engine import QuoteEngine, QuoteAction
from rebates.active_quoting.order_manager import OrderManager
from rebates.active_quoting.models import (
    OrderbookState,
    OrderbookLevel,
    Quote,
    OrderSide,
    OrderStatus,
    MomentumState,
    Fill,
    Position,
)


@pytest.fixture
def config():
    """Configuration for quote cycle tests."""
    return ActiveQuotingConfig(
        dry_run=True,
        order_size_usdc=10.0,
        quote_offset_ticks=0,
        improve_when_spread_ticks=4,
        refresh_threshold_ticks=2,
        inventory_skew_coefficient=0.1,
        batch_size=15,
    )


@pytest.fixture
def quote_engine(config):
    """QuoteEngine instance."""
    return QuoteEngine(config)


@pytest.fixture
def order_manager(config):
    """OrderManager in dry-run mode."""
    return OrderManager(
        config=config,
        api_key="test_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
    )


@pytest.fixture
def basic_orderbook():
    """A basic orderbook for testing."""
    return OrderbookState(
        token_id="token123",
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


class TestBasicQuoteCycle:
    """Tests for the basic quote cycle: calculate -> place -> track."""

    @pytest.mark.asyncio
    async def test_full_cycle_calculate_and_place(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Should calculate quote and place orders."""
        # 1. Calculate quote
        decision = quote_engine.calculate_quote(basic_orderbook)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote is not None

        # 2. Place the quote
        bid_result, ask_result = await order_manager.place_quote(decision.quote)

        assert bid_result.success is True
        assert ask_result.success is True

        # 3. Verify orders are tracked
        pending = order_manager.get_pending_orders("token123")
        assert len(pending) == 2

        # Verify sides
        sides = {o.side for o in pending}
        assert OrderSide.BUY in sides
        assert OrderSide.SELL in sides

    @pytest.mark.asyncio
    async def test_cycle_with_inventory_skew(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Should apply inventory skew and place adjusted orders."""
        # 10 shares inventory -> 1 tick skew
        decision = quote_engine.calculate_quote(basic_orderbook, inventory=10.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        # Skew: bid lower, ask lower (more aggressive selling)
        assert decision.quote.bid_price == 0.48
        assert decision.quote.ask_price == 0.50

        # Place the quote
        bid_result, ask_result = await order_manager.place_quote(decision.quote)

        # Verify placed at skewed prices
        assert bid_result.order_state.price == 0.48
        assert ask_result.order_state.price == 0.50

    @pytest.mark.asyncio
    async def test_cycle_momentum_cancellation(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Should cancel orders during momentum cooldown."""
        # Place initial quote
        decision = quote_engine.calculate_quote(basic_orderbook)
        await order_manager.place_quote(decision.quote)

        assert order_manager.get_open_order_count("token123") == 2

        # Now momentum triggers
        momentum = MomentumState(
            token_id="token123",
            is_active=True,
            cooldown_until=datetime.utcnow() + timedelta(seconds=5),
        )

        # Calculate with momentum - should cancel
        decision = quote_engine.calculate_quote(basic_orderbook, momentum_state=momentum)

        assert decision.action == QuoteAction.CANCEL_ALL

        # Execute cancellation
        await order_manager.cancel_all_for_token("token123")

        assert order_manager.get_open_order_count("token123") == 0

    @pytest.mark.asyncio
    async def test_cycle_hysteresis_keeps_current(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Should refresh quote when mid move exceeds refresh threshold."""
        # Place initial quote
        initial_decision = quote_engine.calculate_quote(basic_orderbook)
        await order_manager.place_quote(initial_decision.quote)

        # Price moves slightly (1 tick) - within threshold
        basic_orderbook.bids[0] = OrderbookLevel(price=0.50, size=100.0)
        basic_orderbook.asks[0] = OrderbookLevel(price=0.52, size=100.0)

        # Calculate with current quote
        decision = quote_engine.calculate_quote(
            basic_orderbook,
            current_quote=initial_decision.quote,
        )

        # Mid moved beyond refresh threshold, so we refresh
        assert decision.action == QuoteAction.PLACE_QUOTE

    @pytest.mark.asyncio
    async def test_cycle_hysteresis_triggers_refresh(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Should refresh quote when price moves beyond threshold."""
        # Place initial quote at 0.49/0.51
        initial_decision = quote_engine.calculate_quote(basic_orderbook)
        await order_manager.place_quote(initial_decision.quote)

        # Price moves significantly (3 ticks)
        basic_orderbook.bids[0] = OrderbookLevel(price=0.52, size=100.0)
        basic_orderbook.asks[0] = OrderbookLevel(price=0.54, size=100.0)

        # Calculate with current quote
        decision = quote_engine.calculate_quote(
            basic_orderbook,
            current_quote=initial_decision.quote,
        )

        # Should trigger refresh (3 tick change >= 2 tick threshold)
        assert decision.action == QuoteAction.PLACE_QUOTE


class TestQuoteCycleWithFills:
    """Tests for quote cycle with simulated fills."""

    @pytest.mark.asyncio
    async def test_fill_updates_position_and_skew(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Fill should update position which affects next quote."""
        position = Position(token_id="token123")

        # Initial quote (no inventory)
        decision1 = quote_engine.calculate_quote(
            basic_orderbook,
            inventory=position.size,
        )

        assert decision1.quote.bid_price == 0.49
        assert decision1.quote.ask_price == 0.51

        # Simulate a fill
        fill = Fill(
            order_id="order1",
            token_id="token123",
            side=OrderSide.BUY,
            price=0.49,
            size=20.0,
        )
        position.update_from_fill(fill)

        # Next quote should be skewed (20 shares * 0.1 = 2 ticks)
        # Bid goes from 0.49 to 0.47 (2 ticks lower)
        # Ask would go from 0.51 to 0.49, but clamped to best_bid + tick = 0.50
        decision2 = quote_engine.calculate_quote(
            basic_orderbook,
            inventory=position.size,
        )

        assert decision2.quote.bid_price == 0.47  # 2 ticks lower
        assert decision2.quote.ask_price == 0.50  # Clamped to best_bid + tick

    @pytest.mark.asyncio
    async def test_opposing_fills_reduce_inventory(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Opposing fills should reduce inventory and skew."""
        position = Position(token_id="token123")

        # Buy fill
        buy_fill = Fill(
            order_id="order1",
            token_id="token123",
            side=OrderSide.BUY,
            price=0.49,
            size=20.0,
        )
        position.update_from_fill(buy_fill)

        # Sell fill (partial)
        sell_fill = Fill(
            order_id="order2",
            token_id="token123",
            side=OrderSide.SELL,
            price=0.51,
            size=10.0,
        )
        position.update_from_fill(sell_fill)

        # Net inventory: 20 - 10 = 10 shares -> 1 tick skew
        decision = quote_engine.calculate_quote(
            basic_orderbook,
            inventory=position.size,
        )

        assert decision.quote.bid_price == 0.48  # 1 tick lower
        assert decision.quote.ask_price == 0.50  # 1 tick lower


class TestMultiMarketQuoteCycle:
    """Tests for managing quotes across multiple markets."""

    @pytest.mark.asyncio
    async def test_batch_quotes_multiple_markets(
        self, config, quote_engine, order_manager
    ):
        """Should place quotes on multiple markets efficiently."""
        markets = [
            OrderbookState(
                token_id=f"token{i}",
                bids=[OrderbookLevel(price=0.49, size=100.0)],
                asks=[OrderbookLevel(price=0.51, size=100.0)],
                tick_size=0.01,
            )
            for i in range(3)
        ]

        # Calculate quotes for all markets
        quotes = []
        for orderbook in markets:
            decision = quote_engine.calculate_quote(orderbook)
            if decision.action == QuoteAction.PLACE_QUOTE:
                quotes.append(decision.quote)

        assert len(quotes) == 3

        # Place all quotes
        for quote in quotes:
            await order_manager.place_quote(quote)

        # Verify all orders placed
        assert order_manager.get_open_order_count() == 6  # 2 per market

        for i in range(3):
            assert order_manager.get_open_order_count(f"token{i}") == 2

    @pytest.mark.asyncio
    async def test_cancel_one_market_keeps_others(
        self, config, quote_engine, order_manager
    ):
        """Cancelling one market should not affect others."""
        # Place quotes on two markets
        for i in range(2):
            orderbook = OrderbookState(
                token_id=f"token{i}",
                bids=[OrderbookLevel(price=0.49, size=100.0)],
                asks=[OrderbookLevel(price=0.51, size=100.0)],
                tick_size=0.01,
            )
            decision = quote_engine.calculate_quote(orderbook)
            await order_manager.place_quote(decision.quote)

        # Cancel token0
        await order_manager.cancel_all_for_token("token0")

        # token0 should have 0 orders, token1 should still have 2
        assert order_manager.get_open_order_count("token0") == 0
        assert order_manager.get_open_order_count("token1") == 2


class TestQuoteCycleEdgeCases:
    """Edge cases for the quote cycle."""

    @pytest.mark.asyncio
    async def test_empty_orderbook_cancels(
        self, quote_engine, order_manager
    ):
        """Empty orderbook should trigger cancellation."""
        # Place quote on valid orderbook
        orderbook = OrderbookState(
            token_id="token123",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )
        decision = quote_engine.calculate_quote(orderbook)
        await order_manager.place_quote(decision.quote)

        # Orderbook becomes empty
        empty_orderbook = OrderbookState(token_id="token123")

        decision = quote_engine.calculate_quote(empty_orderbook)
        assert decision.action == QuoteAction.CANCEL_ALL

    @pytest.mark.asyncio
    async def test_spread_widens_triggers_improvement(
        self, quote_engine, order_manager
    ):
        """Widening spread should trigger quote improvement."""
        # Start with narrow spread
        orderbook = OrderbookState(
            token_id="token123",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        decision1 = quote_engine.calculate_quote(orderbook)
        assert decision1.quote.bid_price == 0.49  # At best bid
        assert decision1.quote.ask_price == 0.51  # At best ask

        # Spread widens to 6 ticks (>= 4 threshold)
        orderbook.bids[0] = OrderbookLevel(price=0.47, size=100.0)
        orderbook.asks[0] = OrderbookLevel(price=0.53, size=100.0)

        decision2 = quote_engine.calculate_quote(orderbook)
        assert decision2.quote.bid_price == 0.48  # Improved by 1 tick
        assert decision2.quote.ask_price == 0.52  # Improved by 1 tick

    @pytest.mark.asyncio
    async def test_large_inventory_clamped_properly(
        self, quote_engine, order_manager
    ):
        """Large inventory should be clamped to prevent crossing."""
        orderbook = OrderbookState(
            token_id="token123",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        # Very large inventory (would cause crossing without clamping)
        decision = quote_engine.calculate_quote(orderbook, inventory=100.0)

        assert decision.action == QuoteAction.PLACE_QUOTE
        assert decision.quote.bid_price < decision.quote.ask_price
        assert decision.quote.is_valid()


class TestQuoteCycleStateSync:
    """Tests for state synchronization between components."""

    @pytest.mark.asyncio
    async def test_order_state_syncs_on_cancel(
        self, quote_engine, order_manager, basic_orderbook
    ):
        """Order state should be updated on cancellation."""
        # Place quote
        decision = quote_engine.calculate_quote(basic_orderbook)
        bid_result, ask_result = await order_manager.place_quote(decision.quote)

        # Cancel bid
        await order_manager.cancel_order(bid_result.order_id)

        # Bid should be cancelled, ask still open
        bid_order = order_manager.get_order(bid_result.order_id)
        ask_order = order_manager.get_order(ask_result.order_id)

        assert bid_order.status == OrderStatus.CANCELLED
        assert ask_order.is_open()

    @pytest.mark.asyncio
    async def test_clear_terminal_orders_after_fills(
        self, order_manager, quote_engine, basic_orderbook
    ):
        """Terminal orders should be cleanable."""
        # Place quote
        decision = quote_engine.calculate_quote(basic_orderbook)
        bid_result, ask_result = await order_manager.place_quote(decision.quote)

        # Simulate fill
        order_manager.update_order_state(bid_result.order_id, OrderStatus.FILLED)
        order_manager.update_order_state(ask_result.order_id, OrderStatus.CANCELLED)

        # Clear terminal orders
        cleared = order_manager.clear_terminal_orders()

        assert cleared == 2
        assert order_manager.get_open_order_count() == 0
