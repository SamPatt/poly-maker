"""
Unit tests for OrderbookManager.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from rebates.active_quoting.orderbook_manager import OrderbookManager
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import OrderbookState, OrderbookLevel


@pytest.fixture
def config():
    """Provide default config for tests."""
    return ActiveQuotingConfig()


@pytest.fixture
def manager(config):
    """Provide an OrderbookManager instance."""
    return OrderbookManager(config)


class TestOrderbookManagerInit:
    """Tests for OrderbookManager initialization."""

    def test_init_default_state(self, config):
        """Manager should initialize with empty state."""
        manager = OrderbookManager(config)
        assert manager.orderbooks == {}
        assert manager.is_connected() is False
        assert manager.last_update_time() is None

    def test_init_with_callbacks(self, config):
        """Manager should accept callbacks."""
        on_book = AsyncMock()
        on_trade = AsyncMock()
        on_tick = AsyncMock()
        on_disconnect = AsyncMock()

        manager = OrderbookManager(
            config,
            on_book_update=on_book,
            on_trade=on_trade,
            on_tick_size_change=on_tick,
            on_disconnect=on_disconnect,
        )

        assert manager.on_book_update is on_book
        assert manager.on_trade is on_trade
        assert manager.on_tick_size_change is on_tick
        assert manager.on_disconnect is on_disconnect


class TestOrderbookManagerState:
    """Tests for OrderbookManager state management."""

    def test_get_orderbook_not_exists(self, manager):
        """get_orderbook should return None for unknown token."""
        assert manager.get_orderbook("unknown_token") is None

    def test_orderbooks_property(self, manager):
        """orderbooks property should return internal state."""
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")
        assert "token1" in manager.orderbooks

    def test_is_connected_without_websocket(self, manager):
        """is_connected should be False without WebSocket."""
        assert manager.is_connected() is False

    def test_is_connected_with_closed_websocket(self, manager):
        """is_connected should be False with closed WebSocket."""
        from websockets import State
        mock_ws = MagicMock()
        mock_ws.state = State.CLOSED
        manager._websocket = mock_ws
        assert manager.is_connected() is False

    def test_is_connected_with_open_websocket(self, manager):
        """is_connected should be True with open WebSocket."""
        from websockets import State
        mock_ws = MagicMock()
        mock_ws.state = State.OPEN
        manager._websocket = mock_ws
        assert manager.is_connected() is True


class TestHandleBookEvent:
    """Tests for handling book (snapshot) events."""

    @pytest.mark.asyncio
    async def test_handle_book_event_basic(self, manager):
        """Should parse and store full orderbook snapshot."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "book",
            "asset_id": "token1",
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.49", "size": "200"},
            ],
            "asks": [
                {"price": "0.51", "size": "150"},
                {"price": "0.52", "size": "250"},
            ],
            "min_tick_size": "0.01",
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book is not None
        assert len(book.bids) == 2
        assert len(book.asks) == 2
        assert book.best_bid == 0.50
        assert book.best_ask == 0.51
        assert book.tick_size == 0.01

    @pytest.mark.asyncio
    async def test_handle_book_event_bids_sorted_descending(self, manager):
        """Bids should be sorted by price descending."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "book",
            "asset_id": "token1",
            "bids": [
                {"price": "0.45", "size": "100"},
                {"price": "0.50", "size": "200"},
                {"price": "0.48", "size": "150"},
            ],
            "asks": [],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert [level.price for level in book.bids] == [0.50, 0.48, 0.45]

    @pytest.mark.asyncio
    async def test_handle_book_event_asks_sorted_ascending(self, manager):
        """Asks should be sorted by price ascending."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "book",
            "asset_id": "token1",
            "bids": [],
            "asks": [
                {"price": "0.55", "size": "100"},
                {"price": "0.51", "size": "200"},
                {"price": "0.53", "size": "150"},
            ],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert [level.price for level in book.asks] == [0.51, 0.53, 0.55]

    @pytest.mark.asyncio
    async def test_handle_book_event_triggers_callback(self, config):
        """Should trigger on_book_update callback."""
        callback = AsyncMock()
        manager = OrderbookManager(config, on_book_update=callback)
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "book",
            "asset_id": "token1",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }

        await manager._handle_message(data)

        callback.assert_called_once()
        call_args = callback.call_args[0]
        assert call_args[0] == "token1"
        assert isinstance(call_args[1], OrderbookState)

    @pytest.mark.asyncio
    async def test_handle_book_event_unsubscribed_token_ignored(self, manager):
        """Events for unsubscribed tokens should be ignored."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "book",
            "asset_id": "token2",  # Not subscribed
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [],
        }

        await manager._handle_message(data)

        # token2 should not be in orderbooks
        assert "token2" not in manager._orderbooks


class TestHandlePriceChangeEvent:
    """Tests for handling price_change (incremental) events."""

    @pytest.mark.asyncio
    async def test_handle_price_change_add_bid(self, manager):
        """Should add a new bid level."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100)],
            asks=[OrderbookLevel(price=0.51, size=100)],
        )

        data = {
            "event_type": "price_change",
            "asset_id": "token1",
            "changes": [
                {"price": "0.50", "size": "200", "side": "BUY"},
            ],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.best_bid == 0.50
        assert book.best_bid_size == 200

    @pytest.mark.asyncio
    async def test_handle_price_change_update_existing_level(self, manager):
        """Should update an existing price level."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.50, size=100)],
            asks=[],
        )

        data = {
            "event_type": "price_change",
            "asset_id": "token1",
            "changes": [
                {"price": "0.50", "size": "500", "side": "BUY"},
            ],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.best_bid == 0.50
        assert book.best_bid_size == 500

    @pytest.mark.asyncio
    async def test_handle_price_change_remove_level_zero_size(self, manager):
        """Should remove level when size is 0."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.50, size=100),
                OrderbookLevel(price=0.49, size=200),
            ],
            asks=[],
        )

        data = {
            "event_type": "price_change",
            "asset_id": "token1",
            "changes": [
                {"price": "0.50", "size": "0", "side": "BUY"},
            ],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.best_bid == 0.49
        assert len(book.bids) == 1

    @pytest.mark.asyncio
    async def test_handle_price_change_add_ask(self, manager):
        """Should add a new ask level."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[],
            asks=[OrderbookLevel(price=0.52, size=100)],
        )

        data = {
            "event_type": "price_change",
            "asset_id": "token1",
            "changes": [
                {"price": "0.51", "size": "150", "side": "SELL"},
            ],
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.best_ask == 0.51
        assert book.best_ask_size == 150


class TestHandleBestBidAskEvent:
    """Tests for handling best_bid_ask events."""

    @pytest.mark.asyncio
    async def test_handle_best_bid_ask_update_existing(self, manager):
        """Should update best bid/ask on existing book."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100)],
            asks=[OrderbookLevel(price=0.51, size=100)],
        )

        data = {
            "event_type": "best_bid_ask",
            "asset_id": "token1",
            "best_bid": {"price": "0.50", "size": "200"},
            "best_ask": {"price": "0.52", "size": "300"},
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        # New best bid should be at top
        assert book.best_bid == 0.50
        assert book.best_bid_size == 200

    @pytest.mark.asyncio
    async def test_handle_best_bid_ask_same_price_update_size(self, manager):
        """Should update size when price matches."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.50, size=100)],
            asks=[OrderbookLevel(price=0.51, size=100)],
        )

        data = {
            "event_type": "best_bid_ask",
            "asset_id": "token1",
            "best_bid": {"price": "0.50", "size": "500"},
            "best_ask": {"price": "0.51", "size": "600"},
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.best_bid_size == 500
        assert book.best_ask_size == 600


class TestHandleLastTradePriceEvent:
    """Tests for handling last_trade_price events."""

    @pytest.mark.asyncio
    async def test_handle_last_trade_price_updates_book(self, manager):
        """Should update last_trade_price in orderbook."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "last_trade_price",
            "asset_id": "token1",
            "price": "0.55",
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.last_trade_price == 0.55

    @pytest.mark.asyncio
    async def test_handle_last_trade_price_triggers_callback(self, config):
        """Should trigger on_trade callback."""
        callback = AsyncMock()
        manager = OrderbookManager(config, on_trade=callback)
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "last_trade_price",
            "asset_id": "token1",
            "price": "0.55",
        }

        await manager._handle_message(data)

        callback.assert_called_once()
        call_args = callback.call_args[0]
        assert call_args[0] == "token1"
        assert call_args[1] == 0.55
        assert isinstance(call_args[2], datetime)


class TestHandleTickSizeChangeEvent:
    """Tests for handling tick_size_change events."""

    @pytest.mark.asyncio
    async def test_handle_tick_size_change_updates_book(self, manager):
        """Should update tick_size in orderbook."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            tick_size=0.01,
        )

        data = {
            "event_type": "tick_size_change",
            "asset_id": "token1",
            "min_tick_size": "0.001",
        }

        await manager._handle_message(data)

        book = manager.get_orderbook("token1")
        assert book.tick_size == 0.001

    @pytest.mark.asyncio
    async def test_handle_tick_size_change_triggers_callback(self, config):
        """Should trigger on_tick_size_change callback."""
        callback = AsyncMock()
        manager = OrderbookManager(config, on_tick_size_change=callback)
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(
            token_id="token1",
            tick_size=0.01,
        )

        data = {
            "event_type": "tick_size_change",
            "asset_id": "token1",
            "min_tick_size": "0.001",
        }

        await manager._handle_message(data)

        callback.assert_called_once_with("token1", 0.001)


class TestUpdateLevel:
    """Tests for _update_level helper method."""

    def test_update_level_add_to_empty_list(self, manager):
        """Should add level to empty list."""
        levels = []
        manager._update_level(levels, 0.50, 100, descending=True)
        assert len(levels) == 1
        assert levels[0].price == 0.50
        assert levels[0].size == 100

    def test_update_level_insert_sorted_bids(self, manager):
        """Should insert bids in descending order."""
        levels = [
            OrderbookLevel(price=0.50, size=100),
            OrderbookLevel(price=0.48, size=100),
        ]
        manager._update_level(levels, 0.49, 200, descending=True)

        assert len(levels) == 3
        assert [l.price for l in levels] == [0.50, 0.49, 0.48]

    def test_update_level_insert_sorted_asks(self, manager):
        """Should insert asks in ascending order."""
        levels = [
            OrderbookLevel(price=0.51, size=100),
            OrderbookLevel(price=0.53, size=100),
        ]
        manager._update_level(levels, 0.52, 200, descending=False)

        assert len(levels) == 3
        assert [l.price for l in levels] == [0.51, 0.52, 0.53]

    def test_update_level_remove_existing(self, manager):
        """Should remove level when size is 0."""
        levels = [
            OrderbookLevel(price=0.50, size=100),
            OrderbookLevel(price=0.49, size=200),
        ]
        manager._update_level(levels, 0.50, 0, descending=True)

        assert len(levels) == 1
        assert levels[0].price == 0.49

    def test_update_level_update_existing(self, manager):
        """Should update existing level."""
        levels = [
            OrderbookLevel(price=0.50, size=100),
        ]
        manager._update_level(levels, 0.50, 500, descending=True)

        assert len(levels) == 1
        assert levels[0].size == 500


class TestParseLevels:
    """Tests for _parse_levels helper method."""

    def test_parse_levels_basic(self, manager):
        """Should parse basic level data."""
        data = [
            {"price": "0.50", "size": "100"},
            {"price": "0.49", "size": "200"},
        ]
        levels = manager._parse_levels(data)

        assert len(levels) == 2
        assert levels[0].price == 0.50
        assert levels[0].size == 100
        assert levels[1].price == 0.49
        assert levels[1].size == 200

    def test_parse_levels_skip_zero_price(self, manager):
        """Should skip levels with zero price."""
        data = [
            {"price": "0", "size": "100"},
            {"price": "0.50", "size": "100"},
        ]
        levels = manager._parse_levels(data)

        assert len(levels) == 1
        assert levels[0].price == 0.50

    def test_parse_levels_skip_zero_size(self, manager):
        """Should skip levels with zero size."""
        data = [
            {"price": "0.50", "size": "0"},
            {"price": "0.49", "size": "100"},
        ]
        levels = manager._parse_levels(data)

        assert len(levels) == 1
        assert levels[0].price == 0.49

    def test_parse_levels_handle_invalid_data(self, manager):
        """Should handle invalid data gracefully."""
        data = [
            {"price": "invalid", "size": "100"},
            {"price": "0.50", "size": "valid"},
            {"price": "0.49", "size": "200"},
            {},
        ]
        levels = manager._parse_levels(data)

        # Only the valid entry should be parsed
        assert len(levels) == 1
        assert levels[0].price == 0.49


class TestTokenManagement:
    """Tests for add_tokens and remove_tokens."""

    @pytest.mark.asyncio
    async def test_add_tokens_initializes_orderbooks(self, manager):
        """add_tokens should initialize orderbook state."""
        await manager.add_tokens(["token1", "token2"])

        assert "token1" in manager._subscribed_tokens
        assert "token2" in manager._subscribed_tokens
        assert "token1" in manager._orderbooks
        assert "token2" in manager._orderbooks

    @pytest.mark.asyncio
    async def test_remove_tokens_clears_state(self, manager):
        """remove_tokens should remove orderbook state."""
        manager._subscribed_tokens = {"token1", "token2"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")
        manager._orderbooks["token2"] = OrderbookState(token_id="token2")

        await manager.remove_tokens(["token1"])

        assert "token1" not in manager._subscribed_tokens
        assert "token2" in manager._subscribed_tokens
        assert "token1" not in manager._orderbooks
        assert "token2" in manager._orderbooks


class TestUnknownEventType:
    """Tests for handling unknown event types."""

    @pytest.mark.asyncio
    async def test_unknown_event_type_ignored(self, manager):
        """Unknown event types should be silently ignored."""
        manager._subscribed_tokens = {"token1"}
        manager._orderbooks["token1"] = OrderbookState(token_id="token1")

        data = {
            "event_type": "unknown_event",
            "asset_id": "token1",
            "data": "something",
        }

        # Should not raise
        await manager._handle_message(data)

        # State should be unchanged
        book = manager.get_orderbook("token1")
        assert book.bids == []
        assert book.asks == []
