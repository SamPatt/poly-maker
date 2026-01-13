"""
Unit tests for Active Quoting data models.
"""
import pytest
from datetime import datetime, timedelta
from rebates.active_quoting.models import (
    Quote,
    OrderbookLevel,
    OrderbookState,
    MomentumState,
    Fill,
    OrderState,
    Position,
    MarketState,
    OrderSide,
    OrderStatus,
)


class TestQuote:
    """Tests for Quote data class."""

    def test_spread_calculation(self):
        """Spread should be ask - bid."""
        quote = Quote(
            token_id="token1",
            bid_price=0.45,
            ask_price=0.55,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.spread() == pytest.approx(0.10)

    def test_spread_ticks(self):
        """Spread in ticks should be calculated correctly."""
        quote = Quote(
            token_id="token1",
            bid_price=0.45,
            ask_price=0.55,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.spread_ticks(0.01) == 10
        assert quote.spread_ticks(0.02) == 5

    def test_spread_ticks_invalid_tick_size(self):
        """spread_ticks should raise for invalid tick_size."""
        quote = Quote(
            token_id="token1",
            bid_price=0.45,
            ask_price=0.55,
            bid_size=100.0,
            ask_size=100.0,
        )
        with pytest.raises(ValueError, match="tick_size must be positive"):
            quote.spread_ticks(0)
        with pytest.raises(ValueError, match="tick_size must be positive"):
            quote.spread_ticks(-0.01)

    def test_mid_price(self):
        """Mid price should be average of bid and ask."""
        quote = Quote(
            token_id="token1",
            bid_price=0.40,
            ask_price=0.60,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.mid_price() == pytest.approx(0.50)

    def test_is_valid_true(self):
        """Valid quote should return True."""
        quote = Quote(
            token_id="token1",
            bid_price=0.45,
            ask_price=0.55,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.is_valid() is True

    def test_is_valid_crossed_spread(self):
        """Crossed spread (bid >= ask) should be invalid."""
        quote = Quote(
            token_id="token1",
            bid_price=0.55,
            ask_price=0.45,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.is_valid() is False

    def test_is_valid_zero_bid(self):
        """Zero bid should be invalid."""
        quote = Quote(
            token_id="token1",
            bid_price=0.0,
            ask_price=0.55,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.is_valid() is False

    def test_is_valid_ask_at_one(self):
        """Ask at 1.0 or above should be invalid."""
        quote = Quote(
            token_id="token1",
            bid_price=0.95,
            ask_price=1.0,
            bid_size=100.0,
            ask_size=100.0,
        )
        assert quote.is_valid() is False

    def test_is_valid_zero_size(self):
        """Zero size should be invalid."""
        quote = Quote(
            token_id="token1",
            bid_price=0.45,
            ask_price=0.55,
            bid_size=0.0,
            ask_size=100.0,
        )
        assert quote.is_valid() is False


class TestOrderbookLevel:
    """Tests for OrderbookLevel data class."""

    def test_valid_level(self):
        """Valid level should be created."""
        level = OrderbookLevel(price=0.50, size=100.0)
        assert level.price == 0.50
        assert level.size == 100.0

    def test_invalid_negative_price(self):
        """Negative price should raise."""
        with pytest.raises(ValueError, match="price must be non-negative"):
            OrderbookLevel(price=-0.01, size=100.0)

    def test_invalid_negative_size(self):
        """Negative size should raise."""
        with pytest.raises(ValueError, match="size must be non-negative"):
            OrderbookLevel(price=0.50, size=-100.0)


class TestOrderbookState:
    """Tests for OrderbookState data class."""

    def test_best_bid_ask(self):
        """Best bid/ask should return top of book."""
        book = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.50, size=100.0),
                OrderbookLevel(price=0.49, size=200.0),
            ],
            asks=[
                OrderbookLevel(price=0.51, size=100.0),
                OrderbookLevel(price=0.52, size=200.0),
            ],
        )
        assert book.best_bid == 0.50
        assert book.best_ask == 0.51
        assert book.best_bid_size == 100.0
        assert book.best_ask_size == 100.0

    def test_empty_book(self):
        """Empty book should return None for best bid/ask."""
        book = OrderbookState(token_id="token1")
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.best_bid_size is None
        assert book.best_ask_size is None

    def test_spread(self):
        """Spread should be calculated correctly."""
        book = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
        )
        assert book.spread() == pytest.approx(0.02)

    def test_spread_empty_book(self):
        """Spread should be None for empty book."""
        book = OrderbookState(token_id="token1")
        assert book.spread() is None

    def test_spread_ticks(self):
        """Spread in ticks should be calculated correctly."""
        book = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )
        assert book.spread_ticks() == 2

    def test_mid_price(self):
        """Mid price should be calculated correctly."""
        book = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.48, size=100.0)],
            asks=[OrderbookLevel(price=0.52, size=100.0)],
        )
        assert book.mid_price() == pytest.approx(0.50)

    def test_bid_depth(self):
        """Bid depth should sum sizes."""
        book = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.50, size=100.0),
                OrderbookLevel(price=0.49, size=200.0),
                OrderbookLevel(price=0.48, size=300.0),
            ],
        )
        assert book.bid_depth(levels=2) == 300.0
        assert book.bid_depth(levels=3) == 600.0

    def test_ask_depth(self):
        """Ask depth should sum sizes."""
        book = OrderbookState(
            token_id="token1",
            asks=[
                OrderbookLevel(price=0.51, size=150.0),
                OrderbookLevel(price=0.52, size=250.0),
            ],
        )
        assert book.ask_depth(levels=2) == 400.0

    def test_is_valid(self):
        """Valid book should have both sides and not be crossed."""
        valid_book = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
        )
        assert valid_book.is_valid() is True

        # Crossed book
        crossed_book = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.51, size=100.0)],
            asks=[OrderbookLevel(price=0.49, size=100.0)],
        )
        assert crossed_book.is_valid() is False

        # Empty book
        empty_book = OrderbookState(token_id="token1")
        assert empty_book.is_valid() is False


class TestMomentumState:
    """Tests for MomentumState data class."""

    def test_in_cooldown_active(self):
        """Should be in cooldown when active and not expired."""
        state = MomentumState(
            token_id="token1",
            is_active=True,
            cooldown_until=datetime.utcnow() + timedelta(seconds=10),
        )
        assert state.in_cooldown() is True

    def test_in_cooldown_expired(self):
        """Should not be in cooldown when expired."""
        state = MomentumState(
            token_id="token1",
            is_active=True,
            cooldown_until=datetime.utcnow() - timedelta(seconds=10),
        )
        assert state.in_cooldown() is False

    def test_in_cooldown_not_active(self):
        """Should not be in cooldown when not active."""
        state = MomentumState(
            token_id="token1",
            is_active=False,
        )
        assert state.in_cooldown() is False

    def test_add_trade(self):
        """Adding trades should update history."""
        state = MomentumState(token_id="token1")
        state.add_trade(0.50)
        state.add_trade(0.51)
        state.add_trade(0.52)

        assert len(state.last_trade_prices) == 3
        assert state.last_trade_prices == [0.50, 0.51, 0.52]

    def test_add_trade_limit(self):
        """Trade history should be limited to 100."""
        state = MomentumState(token_id="token1")
        for i in range(150):
            state.add_trade(float(i) / 100)

        assert len(state.last_trade_prices) == 100
        # Should have kept last 100
        assert state.last_trade_prices[0] == 0.50

    def test_price_change_ticks(self):
        """Price change in ticks should be calculated correctly."""
        state = MomentumState(token_id="token1")
        now = datetime.utcnow()
        state.add_trade(0.50, now - timedelta(milliseconds=200))
        state.add_trade(0.52, now - timedelta(milliseconds=100))
        state.add_trade(0.55, now)

        # All trades within 500ms window
        assert state.price_change_ticks(window_ms=500, tick_size=0.01) == 5

    def test_price_change_ticks_insufficient_trades(self):
        """Should return 0 with fewer than 2 trades."""
        state = MomentumState(token_id="token1")
        assert state.price_change_ticks(window_ms=500, tick_size=0.01) == 0

        state.add_trade(0.50)
        assert state.price_change_ticks(window_ms=500, tick_size=0.01) == 0


class TestFill:
    """Tests for Fill data class."""

    def test_notional(self):
        """Notional should be price * size."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=100.0,
        )
        assert fill.notional == pytest.approx(50.0)

    def test_net_cost_buy(self):
        """Net cost for buy should be notional + fee."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=100.0,
            fee=0.50,
        )
        assert fill.net_cost == pytest.approx(50.50)

    def test_net_cost_sell(self):
        """Net cost for sell should be -notional + fee."""
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.60,
            size=100.0,
            fee=0.60,
        )
        # -60 + 0.60 = -59.40 (received 59.40 after fee)
        assert fill.net_cost == pytest.approx(-59.40)


class TestOrderState:
    """Tests for OrderState data class."""

    def test_filled_size(self):
        """Filled size should be original - remaining."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=30.0,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        assert order.filled_size == pytest.approx(70.0)

    def test_fill_percentage(self):
        """Fill percentage should be calculated correctly."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=25.0,
        )
        assert order.fill_percentage == pytest.approx(75.0)

    def test_fill_percentage_zero_size(self):
        """Fill percentage should be 0 for zero original size."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=0.0,
            remaining_size=0.0,
        )
        assert order.fill_percentage == 0.0

    def test_is_open(self):
        """is_open should be True for active statuses."""
        for status in [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]:
            order = OrderState(
                order_id="order1",
                token_id="token1",
                side=OrderSide.BUY,
                price=0.50,
                original_size=100.0,
                remaining_size=100.0,
                status=status,
            )
            assert order.is_open() is True

    def test_is_done(self):
        """is_done should be True for terminal statuses."""
        for status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED]:
            order = OrderState(
                order_id="order1",
                token_id="token1",
                side=OrderSide.BUY,
                price=0.50,
                original_size=100.0,
                remaining_size=0.0,
                status=status,
            )
            assert order.is_done() is True


class TestPosition:
    """Tests for Position data class."""

    def test_notional(self):
        """Notional should be |size| * avg_entry_price."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
        )
        assert position.notional == pytest.approx(50.0)

    def test_max_liability(self):
        """Max liability should be |size| * avg_entry_price."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
        )
        assert position.max_liability == pytest.approx(50.0)

    def test_update_from_fill_buy_new_position(self):
        """First buy should set avg_entry_price."""
        position = Position(token_id="token1")
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=100.0,
            fee=0.50,
        )
        position.update_from_fill(fill)

        assert position.size == 100.0
        assert position.avg_entry_price == 0.50
        assert position.total_fees_paid == 0.50

    def test_update_from_fill_buy_add_to_position(self):
        """Adding to position should update weighted avg price."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
        )
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.60,
            size=100.0,
        )
        position.update_from_fill(fill)

        assert position.size == 200.0
        # Weighted avg: (100 * 0.50 + 100 * 0.60) / 200 = 0.55
        assert position.avg_entry_price == pytest.approx(0.55)

    def test_update_from_fill_sell_realize_pnl(self):
        """Selling should realize PnL."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
        )
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.60,
            size=50.0,
            fee=0.30,
        )
        position.update_from_fill(fill)

        assert position.size == 50.0
        # PnL: (0.60 - 0.50) * 50 = 5.0
        assert position.realized_pnl == pytest.approx(5.0)
        assert position.total_fees_paid == 0.30


class TestMarketState:
    """Tests for MarketState data class."""

    def test_total_open_order_size(self):
        """Should sum open order sizes for a side."""
        orderbook = OrderbookState(token_id="token1")
        momentum = MomentumState(token_id="token1")
        position = Position(token_id="token1")

        market = MarketState(
            token_id="token1",
            reverse_token_id="token2",
            asset="BTC",
            orderbook=orderbook,
            momentum=momentum,
            position=position,
            open_orders={
                "order1": OrderState(
                    order_id="order1",
                    token_id="token1",
                    side=OrderSide.BUY,
                    price=0.45,
                    original_size=100.0,
                    remaining_size=100.0,
                    status=OrderStatus.OPEN,
                ),
                "order2": OrderState(
                    order_id="order2",
                    token_id="token1",
                    side=OrderSide.BUY,
                    price=0.44,
                    original_size=50.0,
                    remaining_size=50.0,
                    status=OrderStatus.OPEN,
                ),
                "order3": OrderState(
                    order_id="order3",
                    token_id="token1",
                    side=OrderSide.SELL,
                    price=0.55,
                    original_size=75.0,
                    remaining_size=75.0,
                    status=OrderStatus.OPEN,
                ),
            },
        )

        assert market.total_open_order_size(OrderSide.BUY) == 150.0
        assert market.total_open_order_size(OrderSide.SELL) == 75.0

    def test_should_stop_quoting(self):
        """Should stop quoting when drawdown exceeds limit."""
        orderbook = OrderbookState(token_id="token1")
        momentum = MomentumState(token_id="token1")
        position = Position(token_id="token1")

        market = MarketState(
            token_id="token1",
            reverse_token_id="token2",
            asset="BTC",
            orderbook=orderbook,
            momentum=momentum,
            position=position,
            drawdown_usdc=15.0,
        )

        assert market.should_stop_quoting(max_drawdown=20.0) is False
        assert market.should_stop_quoting(max_drawdown=15.0) is True
        assert market.should_stop_quoting(max_drawdown=10.0) is True


class TestOrderSide:
    """Tests for OrderSide enum."""

    def test_order_side_values(self):
        """OrderSide should have correct values."""
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"


class TestOrderStatus:
    """Tests for OrderStatus enum."""

    def test_order_status_values(self):
        """OrderStatus should have correct values."""
        assert OrderStatus.PENDING.value == "PENDING"
        assert OrderStatus.OPEN.value == "OPEN"
        assert OrderStatus.PARTIALLY_FILLED.value == "PARTIALLY_FILLED"
        assert OrderStatus.FILLED.value == "FILLED"
        assert OrderStatus.CANCELLED.value == "CANCELLED"
        assert OrderStatus.EXPIRED.value == "EXPIRED"
        assert OrderStatus.REJECTED.value == "REJECTED"
