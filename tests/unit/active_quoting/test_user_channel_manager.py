"""
Unit tests for UserChannelManager.
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from rebates.active_quoting.user_channel_manager import UserChannelManager
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import OrderState, OrderStatus, OrderSide, Fill


@pytest.fixture
def config():
    """Provide default config for tests."""
    return ActiveQuotingConfig()


@pytest.fixture
def manager(config):
    """Provide a UserChannelManager instance."""
    return UserChannelManager(
        config,
        api_key="test_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
    )


class TestUserChannelManagerInit:
    """Tests for UserChannelManager initialization."""

    def test_init_default_state(self, config):
        """Manager should initialize with empty state."""
        manager = UserChannelManager(
            config,
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
        )
        assert manager.orders == {}
        assert manager.is_connected() is False
        assert manager.last_update_time() is None

    def test_init_with_callbacks(self, config):
        """Manager should accept callbacks."""
        on_fill = AsyncMock()
        on_order = AsyncMock()
        on_disconnect = AsyncMock()

        manager = UserChannelManager(
            config,
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
            on_fill=on_fill,
            on_order_update=on_order,
            on_disconnect=on_disconnect,
        )

        assert manager.on_fill is on_fill
        assert manager.on_order_update is on_order
        assert manager.on_disconnect is on_disconnect


class TestUserChannelManagerState:
    """Tests for UserChannelManager state management."""

    def test_get_order_not_exists(self, manager):
        """get_order should return None for unknown order."""
        assert manager.get_order("unknown_order") is None

    def test_get_order_exists(self, manager):
        """get_order should return order when it exists."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order
        manager._orders_by_token["token1"] = {"order1"}

        result = manager.get_order("order1")
        assert result == order

    def test_get_orders_for_token(self, manager):
        """get_orders_for_token should return all orders for token."""
        order1 = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )
        order2 = OrderState(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            original_size=100.0,
            remaining_size=100.0,
        )
        order3 = OrderState(
            order_id="order3",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.45,
            original_size=100.0,
            remaining_size=100.0,
        )

        manager._orders = {"order1": order1, "order2": order2, "order3": order3}
        manager._orders_by_token = {
            "token1": {"order1", "order2"},
            "token2": {"order3"},
        }

        result = manager.get_orders_for_token("token1")
        assert len(result) == 2
        assert order1 in result
        assert order2 in result
        assert order3 not in result

    def test_get_open_orders(self, manager):
        """get_open_orders should return only open orders."""
        open_order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        filled_order = OrderState(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            original_size=100.0,
            remaining_size=0.0,
            status=OrderStatus.FILLED,
        )

        manager._orders = {"order1": open_order, "order2": filled_order}
        manager._orders_by_token = {"token1": {"order1", "order2"}}

        result = manager.get_open_orders()
        assert len(result) == 1
        assert open_order in result

    def test_get_open_orders_filtered_by_token(self, manager):
        """get_open_orders should filter by token when provided."""
        order1 = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        order2 = OrderState(
            order_id="order2",
            token_id="token2",
            side=OrderSide.BUY,
            price=0.45,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )

        manager._orders = {"order1": order1, "order2": order2}
        manager._orders_by_token = {"token1": {"order1"}, "token2": {"order2"}}

        result = manager.get_open_orders("token1")
        assert len(result) == 1
        assert order1 in result

    def test_is_connected_without_websocket(self, manager):
        """is_connected should be False without WebSocket."""
        assert manager.is_connected() is False

    def test_is_connected_not_authenticated(self, manager):
        """is_connected should be False when not authenticated."""
        from websockets import State
        mock_ws = MagicMock()
        mock_ws.state = State.OPEN
        manager._websocket = mock_ws
        manager._authenticated = False
        assert manager.is_connected() is False

    def test_is_connected_fully_connected(self, manager):
        """is_connected should be True when connected and authenticated."""
        from websockets import State
        mock_ws = MagicMock()
        mock_ws.state = State.OPEN
        manager._websocket = mock_ws
        manager._authenticated = True
        assert manager.is_connected() is True


class TestHandleOrderEvent:
    """Tests for handling order events."""

    @pytest.mark.asyncio
    async def test_handle_order_event_new_order(self, manager):
        """Should create new order state from event."""
        data = {
            "event_type": "order",
            "order_id": "order1",
            "asset_id": "token1",
            "side": "BUY",
            "price": "0.50",
            "original_size": "100",
            "remaining_size": "100",
            "status": "OPEN",
        }

        await manager._handle_message(data)

        order = manager.get_order("order1")
        assert order is not None
        assert order.order_id == "order1"
        assert order.token_id == "token1"
        assert order.side == OrderSide.BUY
        assert order.price == 0.50
        assert order.original_size == 100.0
        assert order.remaining_size == 100.0
        assert order.status == OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_handle_order_event_update_existing(self, manager):
        """Should update existing order state."""
        # Pre-create order
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "order",
            "order_id": "order1",
            "status": "PARTIALLY_FILLED",
            "remaining_size": "50",
        }

        await manager._handle_message(data)

        updated = manager.get_order("order1")
        assert updated.status == OrderStatus.PARTIALLY_FILLED
        assert updated.remaining_size == 50.0

    @pytest.mark.asyncio
    async def test_handle_order_event_cancelled(self, manager):
        """Should handle cancelled order."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "order",
            "order_id": "order1",
            "status": "CANCELLED",
        }

        await manager._handle_message(data)

        # Terminal orders are cleaned up and no longer retrievable
        # This prevents phantom fills from race conditions
        updated = manager.get_order("order1")
        assert updated is None, "Cancelled orders should be cleaned up"

        # Also verify it's not in placed_order_ids
        assert not manager.is_known_order("order1")

    @pytest.mark.asyncio
    async def test_handle_order_event_triggers_callback(self, config):
        """Should trigger on_order_update callback."""
        callback = AsyncMock()
        manager = UserChannelManager(
            config,
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
            on_order_update=callback,
        )

        data = {
            "event_type": "order",
            "order_id": "order1",
            "asset_id": "token1",
            "side": "BUY",
            "price": "0.50",
            "original_size": "100",
            "status": "OPEN",
        }

        await manager._handle_message(data)

        callback.assert_called_once()
        call_arg = callback.call_args[0][0]
        assert isinstance(call_arg, OrderState)
        assert call_arg.order_id == "order1"

    @pytest.mark.asyncio
    async def test_handle_order_event_maps_status_variants(self, manager):
        """Should handle different status string variants."""
        # Test non-terminal statuses (orders remain in tracking)
        non_terminal_tests = [
            ("LIVE", OrderStatus.OPEN),
            ("MATCHED", OrderStatus.PARTIALLY_FILLED),
        ]

        for i, (status_str, expected_status) in enumerate(non_terminal_tests):
            order_id = f"order{i}"
            data = {
                "event_type": "order",
                "order_id": order_id,
                "asset_id": "token1",
                "side": "BUY",
                "price": "0.50",
                "status": status_str,
            }

            await manager._handle_message(data)

            order = manager.get_order(order_id)
            assert order.status == expected_status, f"Failed for status {status_str}"

        # Test terminal status (American spelling) - order should be cleaned up
        data = {
            "event_type": "order",
            "order_id": "order_cancelled",
            "asset_id": "token1",
            "side": "BUY",
            "price": "0.50",
            "status": "CANCELED",  # American spelling
        }
        # First create the order as OPEN so it exists
        manager._orders["order_cancelled"] = OrderState(
            order_id="order_cancelled",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        await manager._handle_message(data)
        # Terminal orders are cleaned up
        assert manager.get_order("order_cancelled") is None


class TestHandleTradeEvent:
    """Tests for handling trade/fill events."""

    @pytest.mark.asyncio
    async def test_handle_trade_event_basic(self, manager):
        """Should process fill and update order."""
        # Pre-create order
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "trade",
            "order_id": "order1",
            "asset_id": "token1",
            "trade_id": "trade1",
            "price": "0.50",
            "size": "50",
            "side": "BUY",
            "fee": "0.25",
        }

        await manager._handle_message(data)

        updated = manager.get_order("order1")
        assert updated.remaining_size == 50.0
        assert updated.status == OrderStatus.PARTIALLY_FILLED
        assert len(updated.fills) == 1
        assert updated.fills[0].size == 50.0

    @pytest.mark.asyncio
    async def test_handle_trade_event_full_fill(self, manager):
        """Should mark order as filled when fully filled."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "trade",
            "order_id": "order1",
            "price": "0.50",
            "size": "100",
        }

        await manager._handle_message(data)

        updated = manager.get_order("order1")
        assert updated.remaining_size == 0.0
        assert updated.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_handle_trade_event_triggers_callback(self, config):
        """Should trigger on_fill callback."""
        callback = AsyncMock()
        manager = UserChannelManager(
            config,
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
            on_fill=callback,
        )

        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "trade",
            "order_id": "order1",
            "asset_id": "token1",
            "price": "0.50",
            "size": "50",
        }

        await manager._handle_message(data)

        callback.assert_called_once()
        fill = callback.call_args[0][0]
        assert isinstance(fill, Fill)
        assert fill.order_id == "order1"
        assert fill.size == 50.0

    @pytest.mark.asyncio
    async def test_handle_trade_event_inherits_side_from_order(self, manager):
        """Should inherit side from order when not in trade event."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            original_size=100.0,
            remaining_size=100.0,
        )
        manager._orders["order1"] = order

        data = {
            "event_type": "trade",
            "order_id": "order1",
            "price": "0.55",
            "size": "50",
            # No side field
        }

        await manager._handle_message(data)

        assert order.fills[0].side == OrderSide.SELL


class TestOrderManagement:
    """Tests for add_order, remove_order, clear_orders."""

    def test_add_order(self, manager):
        """add_order should add to tracking."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )

        manager.add_order(order)

        assert "order1" in manager._orders
        assert "order1" in manager._orders_by_token.get("token1", set())

    def test_remove_order(self, manager):
        """remove_order should remove from tracking."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )
        manager._orders["order1"] = order
        manager._orders_by_token["token1"] = {"order1"}

        removed = manager.remove_order("order1")

        assert removed == order
        assert "order1" not in manager._orders
        assert "order1" not in manager._orders_by_token.get("token1", set())

    def test_remove_order_not_exists(self, manager):
        """remove_order should return None for unknown order."""
        result = manager.remove_order("unknown")
        assert result is None

    def test_clear_orders(self, manager):
        """clear_orders should remove all orders."""
        order1 = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )
        order2 = OrderState(
            order_id="order2",
            token_id="token2",
            side=OrderSide.SELL,
            price=0.55,
            original_size=100.0,
            remaining_size=100.0,
        )
        manager._orders = {"order1": order1, "order2": order2}
        manager._orders_by_token = {"token1": {"order1"}, "token2": {"order2"}}

        manager.clear_orders()

        assert manager._orders == {}
        assert manager._orders_by_token == {}


class TestReconcileWithApiOrders:
    """Tests for reconcile_with_api_orders."""

    def test_reconcile_updates_status_mismatch(self, manager):
        """Should update order when API status differs."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        api_orders = [
            {
                "id": "order1",
                "status": "FILLED",
                "remaining_size": "0",
            }
        ]

        manager.reconcile_with_api_orders(api_orders)

        assert order.status == OrderStatus.FILLED
        assert order.remaining_size == 0.0

    def test_reconcile_adds_missing_order(self, manager):
        """Should add order that exists in API but not locally."""
        api_orders = [
            {
                "id": "order1",
                "asset_id": "token1",
                "side": "BUY",
                "price": "0.50",
                "original_size": "100",
                "remaining_size": "100",
                "status": "OPEN",
            }
        ]

        manager.reconcile_with_api_orders(api_orders)

        order = manager.get_order("order1")
        assert order is not None
        assert order.status == OrderStatus.OPEN

    def test_reconcile_marks_missing_api_orders_cancelled(self, manager):
        """Should mark local open orders as cancelled if not in API."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        api_orders = []  # Empty - order not returned by API

        manager.reconcile_with_api_orders(api_orders)

        assert order.status == OrderStatus.CANCELLED

    def test_reconcile_ignores_closed_orders_not_in_api(self, manager):
        """Should not change status of already closed orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=0.0,
            status=OrderStatus.FILLED,
        )
        manager._orders["order1"] = order

        api_orders = []  # Filled orders may not be returned

        manager.reconcile_with_api_orders(api_orders)

        # Should still be FILLED, not changed to CANCELLED
        assert order.status == OrderStatus.FILLED


class TestAuthHandling:
    """Tests for authentication handling."""

    @pytest.mark.asyncio
    async def test_handle_auth_success(self, manager):
        """Should set authenticated on auth success."""
        manager._authenticated = False

        data = {
            "type": "auth",
            "status": "success",
        }

        await manager._handle_message(data)

        assert manager._authenticated is True

    @pytest.mark.asyncio
    async def test_handle_auth_error(self, manager):
        """Should clear authenticated on error."""
        manager._authenticated = True

        data = {
            "error": "Invalid API key",
        }

        await manager._handle_message(data)

        assert manager._authenticated is False


class TestDirectOrderFormat:
    """Tests for handling messages without event_type wrapper."""

    @pytest.mark.asyncio
    async def test_handle_direct_order_object(self, manager):
        """Should handle order object without event_type."""
        data = {
            "order_id": "order1",
            "asset_id": "token1",
            "side": "BUY",
            "price": "0.50",
            "original_size": "100",
            "status": "OPEN",
        }

        await manager._handle_message(data)

        order = manager.get_order("order1")
        assert order is not None
        assert order.status == OrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_handle_direct_trade_object(self, manager):
        """Should handle trade object with trade_id (without event_type)."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
        )
        manager._orders["order1"] = order

        data = {
            "order_id": "order1",
            "trade_id": "trade123",  # trade_id indicates this is a trade
            "price": "0.50",
            "size": "50",
        }

        await manager._handle_message(data)

        assert len(order.fills) == 1


class TestPhantomFillPrevention:
    """Tests for preventing phantom fills from race conditions.

    Phantom fills occur when:
    1. API reconciliation marks an order as cancelled (not in API response)
    2. WebSocket trade message arrives milliseconds later for that order
    3. Without proper checks, the fill would be recorded but never confirmed

    The fix has two parts:
    - is_known_order() rejects terminal (cancelled/filled) orders
    - _maybe_cleanup_order() removes terminal orders from tracking
    """

    def test_is_known_order_rejects_cancelled(self, manager):
        """is_known_order should return False for cancelled orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.CANCELLED,
        )
        manager._orders["order1"] = order

        # Even though order exists in _orders, it's terminal so not "known"
        assert manager.is_known_order("order1") is False

    def test_is_known_order_rejects_filled(self, manager):
        """is_known_order should return False for fully filled orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=0.0,
            status=OrderStatus.FILLED,
        )
        manager._orders["order1"] = order

        assert manager.is_known_order("order1") is False

    def test_is_known_order_accepts_open(self, manager):
        """is_known_order should return True for open orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        assert manager.is_known_order("order1") is True

    def test_is_known_order_accepts_partially_filled(self, manager):
        """is_known_order should return True for partially filled orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=50.0,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        manager._orders["order1"] = order

        assert manager.is_known_order("order1") is True

    def test_cleanup_removes_cancelled_order(self, manager):
        """_maybe_cleanup_order should remove cancelled orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.CANCELLED,
        )
        manager._orders["order1"] = order
        manager._orders_by_token["token1"] = {"order1"}
        manager._placed_order_ids.add("order1")

        manager._maybe_cleanup_order("order1", "token1")

        assert "order1" not in manager._orders
        assert "order1" not in manager._orders_by_token.get("token1", set())
        assert "order1" not in manager._placed_order_ids

    def test_cleanup_keeps_open_order(self, manager):
        """_maybe_cleanup_order should not remove open orders."""
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.OPEN,
        )
        manager._orders["order1"] = order

        manager._maybe_cleanup_order("order1", "token1")

        assert "order1" in manager._orders

    def test_placed_order_id_with_terminal_status_rejected(self, manager):
        """is_known_order should reject placed order if it has terminal status."""
        # Order is in placed_order_ids (just placed)
        manager._placed_order_ids.add("order1")

        # But also has a terminal status in _orders (got cancelled)
        order = OrderState(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=100.0,
            remaining_size=100.0,
            status=OrderStatus.CANCELLED,
        )
        manager._orders["order1"] = order

        # Should be rejected because terminal status takes precedence
        assert manager.is_known_order("order1") is False
