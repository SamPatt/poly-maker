"""
Unit tests for OrderManager.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
import asyncio

from rebates.active_quoting.order_manager import (
    OrderManager,
    OrderResult,
    BatchOrderResult,
    FeeRateCache,
)
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import (
    OrderState,
    OrderStatus,
    OrderSide,
    Quote,
)


@pytest.fixture
def config():
    """Default dry-run configuration for tests."""
    return ActiveQuotingConfig(
        dry_run=True,
        order_size_usdc=10.0,
        batch_size=15,
        fee_cache_ttl_seconds=300,
        post_only=True,
    )


@pytest.fixture
def order_manager(config):
    """OrderManager instance in dry-run mode."""
    return OrderManager(
        config=config,
        api_key="test_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
    )


@pytest.fixture
def live_config():
    """Configuration for live mode tests."""
    return ActiveQuotingConfig(
        dry_run=False,
        order_size_usdc=10.0,
        batch_size=15,
        fee_cache_ttl_seconds=300,
        post_only=True,
    )


class TestOrderManagerDryRun:
    """Tests for dry-run mode order placement."""

    @pytest.mark.asyncio
    async def test_place_order_dry_run(self, order_manager):
        """Should simulate order placement in dry run mode."""
        result = await order_manager.place_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )

        assert result.success is True
        assert result.order_id is not None
        assert result.order_id.startswith("sim_")
        assert result.order_state is not None
        assert result.order_state.status == OrderStatus.OPEN
        assert result.order_state.price == 0.50
        assert result.order_state.original_size == 10.0

    @pytest.mark.asyncio
    async def test_place_quote_dry_run(self, order_manager):
        """Should place both bid and ask in dry run mode."""
        quote = Quote(
            token_id="token123",
            bid_price=0.49,
            ask_price=0.51,
            bid_size=10.0,
            ask_size=10.0,
        )

        bid_result, ask_result = await order_manager.place_quote(quote)

        assert bid_result.success is True
        assert ask_result.success is True
        assert bid_result.order_state.side == OrderSide.BUY
        assert ask_result.order_state.side == OrderSide.SELL
        assert bid_result.order_state.price == 0.49
        assert ask_result.order_state.price == 0.51

    @pytest.mark.asyncio
    async def test_cancel_order_dry_run(self, order_manager):
        """Should cancel order in dry run mode."""
        # First place an order
        result = await order_manager.place_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        order_id = result.order_id

        # Then cancel it
        cancelled = await order_manager.cancel_order(order_id)

        assert cancelled is True
        assert order_manager.get_order(order_id).status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_all_for_token_dry_run(self, order_manager):
        """Should cancel all orders for a token in dry run mode."""
        # Place multiple orders
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        await order_manager.place_order("token123", OrderSide.SELL, 0.51, 10.0)
        await order_manager.place_order("token456", OrderSide.BUY, 0.50, 10.0)

        # Cancel all for token123
        cancelled = await order_manager.cancel_all_for_token("token123")

        assert cancelled == 2

        # token456 order should still be open
        token456_orders = order_manager.get_pending_orders("token456")
        assert len(token456_orders) == 1
        assert token456_orders[0].is_open()


class TestOrderManagerBatchOrders:
    """Tests for batch order placement."""

    @pytest.mark.asyncio
    async def test_place_orders_batch_empty(self, order_manager):
        """Should handle empty batch."""
        result = await order_manager.place_orders_batch([])

        assert len(result.successful_orders) == 0
        assert len(result.failed_orders) == 0
        assert result.all_succeeded is False

    @pytest.mark.asyncio
    async def test_place_orders_batch_single(self, order_manager):
        """Should handle single order batch."""
        orders = [
            ("token123", OrderSide.BUY, 0.50, 10.0, False),
        ]

        result = await order_manager.place_orders_batch(orders)

        assert len(result.successful_orders) == 1
        assert len(result.failed_orders) == 0
        assert result.all_succeeded is True

    @pytest.mark.asyncio
    async def test_place_orders_batch_multiple(self, order_manager):
        """Should place multiple orders in batch."""
        orders = [
            ("token123", OrderSide.BUY, 0.49, 10.0, False),
            ("token123", OrderSide.SELL, 0.51, 10.0, False),
            ("token456", OrderSide.BUY, 0.50, 10.0, False),
        ]

        result = await order_manager.place_orders_batch(orders)

        assert len(result.successful_orders) == 3
        assert len(result.failed_orders) == 0
        assert result.all_succeeded is True

    @pytest.mark.asyncio
    async def test_place_orders_batch_respects_batch_size(self, config):
        """Should split orders into batches of batch_size."""
        config.batch_size = 2  # Small batch size for testing
        order_manager = OrderManager(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
        )

        # 5 orders should be processed in 3 batches (2, 2, 1)
        orders = [
            (f"token{i}", OrderSide.BUY, 0.50, 10.0, False)
            for i in range(5)
        ]

        result = await order_manager.place_orders_batch(orders)

        assert len(result.successful_orders) == 5


class TestOrderManagerFeeCache:
    """Tests for fee rate caching."""

    @pytest.mark.asyncio
    async def test_fee_cache_hit(self, order_manager):
        """Should return cached fee rate when valid."""
        import time

        # Manually add to cache
        order_manager._fee_cache["token123"] = FeeRateCache(
            fee_rate_bps=100,
            cached_at=time.time(),
        )

        fee_rate = await order_manager.get_fee_rate("token123")

        assert fee_rate == 100

    @pytest.mark.asyncio
    async def test_fee_cache_miss(self, order_manager):
        """Should fetch fee rate when not cached."""
        with patch.object(order_manager, '_fetch_fee_rate', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = 150

            fee_rate = await order_manager.get_fee_rate("token999")

            assert fee_rate == 150
            mock_fetch.assert_called_once_with("token999")

    @pytest.mark.asyncio
    async def test_fee_cache_expired(self, order_manager):
        """Should refetch fee rate when cache expires."""
        import time

        # Add expired cache entry
        order_manager._fee_cache["token123"] = FeeRateCache(
            fee_rate_bps=100,
            cached_at=time.time() - 1000,  # Expired
        )

        with patch.object(order_manager, '_fetch_fee_rate', new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = 200

            fee_rate = await order_manager.get_fee_rate("token123")

            assert fee_rate == 200
            mock_fetch.assert_called_once()

    def test_clear_fee_cache_specific(self, order_manager):
        """Should clear specific token from cache."""
        import time

        order_manager._fee_cache["token1"] = FeeRateCache(100, time.time())
        order_manager._fee_cache["token2"] = FeeRateCache(200, time.time())

        order_manager.clear_fee_cache("token1")

        assert "token1" not in order_manager._fee_cache
        assert "token2" in order_manager._fee_cache

    def test_clear_fee_cache_all(self, order_manager):
        """Should clear all cache entries."""
        import time

        order_manager._fee_cache["token1"] = FeeRateCache(100, time.time())
        order_manager._fee_cache["token2"] = FeeRateCache(200, time.time())

        order_manager.clear_fee_cache()

        assert len(order_manager._fee_cache) == 0


class TestOrderManagerStateManagement:
    """Tests for order state management."""

    @pytest.mark.asyncio
    async def test_get_pending_orders(self, order_manager):
        """Should return only open orders."""
        # Place some orders
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        result = await order_manager.place_order("token123", OrderSide.SELL, 0.51, 10.0)

        # Cancel one
        await order_manager.cancel_order(result.order_id)

        pending = order_manager.get_pending_orders()
        assert len(pending) == 1
        assert pending[0].side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_get_pending_orders_by_token(self, order_manager):
        """Should filter by token ID."""
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        await order_manager.place_order("token456", OrderSide.BUY, 0.50, 10.0)

        pending_123 = order_manager.get_pending_orders("token123")
        pending_456 = order_manager.get_pending_orders("token456")

        assert len(pending_123) == 1
        assert len(pending_456) == 1
        assert pending_123[0].token_id == "token123"
        assert pending_456[0].token_id == "token456"

    @pytest.mark.asyncio
    async def test_get_order(self, order_manager):
        """Should return order by ID."""
        result = await order_manager.place_order("token123", OrderSide.BUY, 0.50, 10.0)

        order = order_manager.get_order(result.order_id)

        assert order is not None
        assert order.order_id == result.order_id
        assert order.price == 0.50

    def test_get_order_not_found(self, order_manager):
        """Should return None for unknown order ID."""
        order = order_manager.get_order("nonexistent")
        assert order is None

    @pytest.mark.asyncio
    async def test_update_order_state(self, order_manager):
        """Should update order status."""
        result = await order_manager.place_order("token123", OrderSide.BUY, 0.50, 10.0)

        order_manager.update_order_state(result.order_id, OrderStatus.FILLED)

        order = order_manager.get_order(result.order_id)
        assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_remove_order(self, order_manager):
        """Should remove order from tracking."""
        result = await order_manager.place_order("token123", OrderSide.BUY, 0.50, 10.0)

        removed = order_manager.remove_order(result.order_id)

        assert removed is not None
        assert removed.order_id == result.order_id
        assert order_manager.get_order(result.order_id) is None

    @pytest.mark.asyncio
    async def test_clear_terminal_orders(self, order_manager):
        """Should remove orders in terminal states."""
        # Place orders
        result1 = await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        result2 = await order_manager.place_order("token123", OrderSide.SELL, 0.51, 10.0)

        # Cancel one, fill another
        await order_manager.cancel_order(result1.order_id)
        order_manager.update_order_state(result2.order_id, OrderStatus.FILLED)

        # Place a new open order
        result3 = await order_manager.place_order("token123", OrderSide.BUY, 0.50, 10.0)

        # Clear terminal orders
        cleared = order_manager.clear_terminal_orders()

        assert cleared == 2
        assert order_manager.get_order(result1.order_id) is None
        assert order_manager.get_order(result2.order_id) is None
        assert order_manager.get_order(result3.order_id) is not None


class TestOrderManagerReplaceQuote:
    """Tests for quote replacement."""

    @pytest.mark.asyncio
    async def test_replace_quote(self, order_manager):
        """Should cancel old quote and place new one."""
        old_quote = Quote(
            token_id="token123",
            bid_price=0.48,
            ask_price=0.52,
            bid_size=10.0,
            ask_size=10.0,
        )

        new_quote = Quote(
            token_id="token123",
            bid_price=0.49,
            ask_price=0.51,
            bid_size=10.0,
            ask_size=10.0,
        )

        # Place old quote
        await order_manager.place_quote(old_quote)

        # Replace with new quote
        bid_result, ask_result = await order_manager.replace_quote(old_quote, new_quote)

        assert bid_result.success is True
        assert ask_result.success is True

        # Verify new prices
        pending = order_manager.get_pending_orders("token123")
        prices = {o.price for o in pending}
        assert 0.49 in prices
        assert 0.51 in prices


class TestOrderManagerUtilities:
    """Tests for utility methods."""

    @pytest.mark.asyncio
    async def test_get_open_order_count(self, order_manager):
        """Should return count of open orders."""
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        await order_manager.place_order("token123", OrderSide.SELL, 0.51, 10.0)
        await order_manager.place_order("token456", OrderSide.BUY, 0.50, 10.0)

        assert order_manager.get_open_order_count() == 3
        assert order_manager.get_open_order_count("token123") == 2
        assert order_manager.get_open_order_count("token456") == 1

    @pytest.mark.asyncio
    async def test_get_open_order_size(self, order_manager):
        """Should return total size of open orders."""
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        await order_manager.place_order("token123", OrderSide.BUY, 0.48, 20.0)
        await order_manager.place_order("token123", OrderSide.SELL, 0.51, 15.0)

        assert order_manager.get_open_order_size("token123") == pytest.approx(45.0)
        assert order_manager.get_open_order_size("token123", OrderSide.BUY) == pytest.approx(30.0)
        assert order_manager.get_open_order_size("token123", OrderSide.SELL) == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_cancel_all(self, order_manager):
        """Should cancel all open orders."""
        await order_manager.place_order("token123", OrderSide.BUY, 0.49, 10.0)
        await order_manager.place_order("token456", OrderSide.BUY, 0.50, 10.0)
        await order_manager.place_order("token789", OrderSide.SELL, 0.51, 10.0)

        cancelled = await order_manager.cancel_all()

        assert cancelled == 3
        assert order_manager.get_open_order_count() == 0


class TestOrderManagerLiveMode:
    """Tests for live mode behavior."""

    @pytest.mark.asyncio
    async def test_live_mode_without_client_fails(self, live_config):
        """Should fail to place orders in live mode without client."""
        order_manager = OrderManager(
            config=live_config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            poly_client=None,
        )

        result = await order_manager.place_order(
            token_id="token123",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )

        assert result.success is False
        assert "No PolymarketClient" in result.error_msg

    @pytest.mark.asyncio
    async def test_live_mode_with_mock_client(self, live_config):
        """Should place orders in live mode with client."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {
            "orderID": "live_order_123",
        }

        order_manager = OrderManager(
            config=live_config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            poly_client=mock_client,
        )

        # Mock fee rate fetch
        with patch.object(order_manager, 'get_fee_rate', new_callable=AsyncMock) as mock_fee:
            mock_fee.return_value = 100

            result = await order_manager.place_order(
                token_id="token123",
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
            )

        assert result.success is True
        assert result.order_id == "live_order_123"
        mock_client.create_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_mode_order_error(self, live_config):
        """Should handle order errors in live mode."""
        mock_client = MagicMock()
        mock_client.create_order.return_value = {
            "success": False,
            "errorMsg": "Insufficient balance",
        }

        order_manager = OrderManager(
            config=live_config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            poly_client=mock_client,
        )

        with patch.object(order_manager, 'get_fee_rate', new_callable=AsyncMock) as mock_fee:
            mock_fee.return_value = 100

            result = await order_manager.place_order(
                token_id="token123",
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
            )

        assert result.success is False
        assert "Insufficient balance" in result.error_msg


class TestOrderResult:
    """Tests for OrderResult dataclass."""

    def test_success_result(self):
        """Should create successful result."""
        result = OrderResult(
            success=True,
            order_id="order123",
        )

        assert result.success is True
        assert result.order_id == "order123"
        assert result.error_msg == ""

    def test_failure_result(self):
        """Should create failure result."""
        result = OrderResult(
            success=False,
            error_msg="Something went wrong",
        )

        assert result.success is False
        assert result.order_id is None
        assert result.error_msg == "Something went wrong"


class TestBatchOrderResult:
    """Tests for BatchOrderResult dataclass."""

    def test_all_succeeded_true(self):
        """Should return True when all orders succeeded."""
        result = BatchOrderResult(
            successful_orders=[OrderResult(success=True, order_id="1")],
            failed_orders=[],
        )

        assert result.all_succeeded is True

    def test_all_succeeded_false_with_failures(self):
        """Should return False when there are failures."""
        result = BatchOrderResult(
            successful_orders=[OrderResult(success=True, order_id="1")],
            failed_orders=[OrderResult(success=False, error_msg="error")],
        )

        assert result.all_succeeded is False

    def test_all_succeeded_false_empty(self):
        """Should return False when empty."""
        result = BatchOrderResult()

        assert result.all_succeeded is False


class TestOrderManagerCleanup:
    """Tests for cleanup and resource management."""

    @pytest.mark.asyncio
    async def test_close_session(self, order_manager):
        """Should close HTTP session properly."""
        # Ensure session is created
        await order_manager._ensure_session()
        assert order_manager._session is not None

        # Close it
        await order_manager.close()
        assert order_manager._session is None

    @pytest.mark.asyncio
    async def test_close_when_no_session(self, order_manager):
        """Should handle close when no session exists."""
        # Should not raise
        await order_manager.close()
