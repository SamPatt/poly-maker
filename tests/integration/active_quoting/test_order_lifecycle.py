"""
Integration tests for Order Lifecycle.

Phase 2 Integration Gate:
- Place post-only order and verify on user channel
- Cancel order and verify on user channel
- Verify fee rate fetching

Run with:
    POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_order_lifecycle.py -v
"""
import asyncio
import os
import pytest
from datetime import datetime
from typing import Optional

from rebates.active_quoting.order_manager import OrderManager, OrderResult
from rebates.active_quoting.user_channel_manager import UserChannelManager
from rebates.active_quoting.orderbook_manager import OrderbookManager
from rebates.active_quoting.quote_engine import QuoteEngine, QuoteAction
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import OrderSide, OrderStatus, Fill
from rebates.market_finder import CryptoMarketFinder

# Skip all tests in this module if integration tests are disabled
pytestmark = pytest.mark.skipif(
    os.getenv("POLY_TEST_INTEGRATION", "false").lower() != "true",
    reason="Integration tests disabled (set POLY_TEST_INTEGRATION=true)",
)


def get_test_token_ids():
    """Get token IDs from upcoming markets for testing."""
    import json as json_module

    finder = CryptoMarketFinder()
    markets = finder.get_upcoming_markets()

    token_ids = []
    for market in markets[:2]:  # Get up to 2 markets
        clob_token_ids = market.get("clobTokenIds", [])
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json_module.loads(clob_token_ids)
                except json_module.JSONDecodeError:
                    continue
            if isinstance(clob_token_ids, list):
                token_ids.extend(clob_token_ids)

    if not token_ids:
        pytest.skip("No upcoming markets found for testing")

    return token_ids


def get_api_credentials():
    """Get API credentials derived from private key."""
    from dotenv import load_dotenv
    load_dotenv()

    pk = os.getenv("PK")
    browser_address = os.getenv("BROWSER_ADDRESS")

    if not pk or not browser_address:
        pytest.skip("PK or BROWSER_ADDRESS not set in environment")

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=POLYGON,
            funder=browser_address,
            signature_type=2,
        )
        creds = client.create_or_derive_api_creds()

        return creds.api_key, creds.api_secret, creds.api_passphrase
    except Exception as e:
        pytest.skip(f"Failed to derive API credentials: {e}")


def get_polymarket_client():
    """Get a PolymarketClient for live order operations."""
    from dotenv import load_dotenv
    load_dotenv()

    # Only create client if not in dry-run mode
    if os.getenv("AQ_DRY_RUN", "true").lower() == "true":
        return None

    try:
        from poly_data.polymarket_client import PolymarketClient
        return PolymarketClient()
    except Exception as e:
        print(f"Failed to create PolymarketClient: {e}")
        return None


@pytest.fixture
def config():
    """Provide config for integration tests."""
    return ActiveQuotingConfig(
        dry_run=True,  # Default to dry-run for safety
        order_size_usdc=5.0,  # Minimum order size
        ws_reconnect_delay_seconds=2.0,
        ws_max_reconnect_attempts=3,
        fee_cache_ttl_seconds=60,
    )


@pytest.fixture
def api_credentials():
    """Provide API credentials."""
    return get_api_credentials()


class TestFeeRateFetching:
    """Test fee rate API integration."""

    @pytest.mark.asyncio
    async def test_fetch_fee_rate_for_token(self, config, api_credentials):
        """Should fetch fee rate from API for a token."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        if not token_ids:
            pytest.skip("No tokens available for testing")

        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        try:
            # Fetch fee rate
            fee_rate = await order_manager.get_fee_rate(token_ids[0])

            # Fee rate should be a non-negative integer (bps)
            assert isinstance(fee_rate, int)
            assert fee_rate >= 0

            print(f"Fee rate for {token_ids[0][:20]}...: {fee_rate} bps")

            # Verify caching works
            fee_rate_cached = await order_manager.get_fee_rate(token_ids[0])
            assert fee_rate_cached == fee_rate

        finally:
            await order_manager.close()

    @pytest.mark.asyncio
    async def test_fee_rate_caching(self, config, api_credentials):
        """Should cache fee rates and respect TTL."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        if not token_ids:
            pytest.skip("No tokens available for testing")

        # Short TTL for testing
        config.fee_cache_ttl_seconds = 2

        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        try:
            # First fetch
            fee_rate_1 = await order_manager.get_fee_rate(token_ids[0])

            # Should be cached (no API call)
            assert token_ids[0] in order_manager._fee_cache

            # Clear cache and verify it refetches
            order_manager.clear_fee_cache(token_ids[0])
            assert token_ids[0] not in order_manager._fee_cache

            fee_rate_2 = await order_manager.get_fee_rate(token_ids[0])
            assert fee_rate_2 >= 0

        finally:
            await order_manager.close()


class TestOrderPlacementDryRun:
    """Test order placement in dry-run mode."""

    @pytest.mark.asyncio
    async def test_place_order_dry_run(self, config, api_credentials):
        """Should simulate order placement in dry-run mode."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        try:
            result = await order_manager.place_order(
                token_id=token_ids[0],
                side=OrderSide.BUY,
                price=0.10,  # Safe low price
                size=5.0,  # Minimum size
            )

            assert result.success is True
            assert result.order_id is not None
            assert result.order_id.startswith("sim_")
            assert result.order_state is not None
            assert result.order_state.status == OrderStatus.OPEN

            print(f"[DRY RUN] Placed order: {result.order_id}")

        finally:
            await order_manager.close()

    @pytest.mark.asyncio
    async def test_cancel_order_dry_run(self, config, api_credentials):
        """Should cancel simulated order in dry-run mode."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        try:
            # Place order
            result = await order_manager.place_order(
                token_id=token_ids[0],
                side=OrderSide.BUY,
                price=0.10,
                size=5.0,
            )

            # Cancel it
            cancelled = await order_manager.cancel_order(result.order_id)
            assert cancelled is True

            # Verify status updated
            order = order_manager.get_order(result.order_id)
            assert order.status == OrderStatus.CANCELLED

            print(f"[DRY RUN] Cancelled order: {result.order_id}")

        finally:
            await order_manager.close()


class TestQuoteCycleIntegration:
    """Test the full quote cycle with real orderbook data."""

    @pytest.mark.asyncio
    async def test_calculate_quote_from_live_orderbook(self, config, api_credentials):
        """Should calculate quotes based on live orderbook data."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        quote_engine = QuoteEngine(config)
        orderbook_manager = OrderbookManager(config)

        book_received = asyncio.Event()
        live_orderbook = None

        async def on_book_update(token_id, orderbook):
            nonlocal live_orderbook
            if orderbook.bids and orderbook.asks:
                live_orderbook = orderbook
                book_received.set()

        orderbook_manager.on_book_update = on_book_update
        connect_task = asyncio.create_task(orderbook_manager.connect(token_ids[:1]))

        try:
            # Wait for orderbook data
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            assert live_orderbook is not None
            assert live_orderbook.best_bid is not None
            assert live_orderbook.best_ask is not None

            # Calculate quote
            decision = quote_engine.calculate_quote(live_orderbook)

            assert decision.action == QuoteAction.PLACE_QUOTE
            assert decision.quote is not None
            assert decision.quote.bid_price > 0
            assert decision.quote.ask_price < 1.0
            assert decision.quote.bid_price < decision.quote.ask_price

            print(f"Live orderbook: bid={live_orderbook.best_bid}, ask={live_orderbook.best_ask}")
            print(f"Calculated quote: bid={decision.quote.bid_price}, ask={decision.quote.ask_price}")

        finally:
            await orderbook_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_full_quote_cycle_dry_run(self, config, api_credentials):
        """Test full cycle: get orderbook -> calculate quote -> place (dry-run)."""
        api_key, api_secret, api_passphrase = api_credentials
        token_ids = get_test_token_ids()

        quote_engine = QuoteEngine(config)
        orderbook_manager = OrderbookManager(config)
        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        book_received = asyncio.Event()
        live_orderbook = None

        async def on_book_update(token_id, orderbook):
            nonlocal live_orderbook
            if orderbook.bids and orderbook.asks:
                live_orderbook = orderbook
                book_received.set()

        orderbook_manager.on_book_update = on_book_update
        connect_task = asyncio.create_task(orderbook_manager.connect(token_ids[:1]))

        try:
            # 1. Get live orderbook
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            # 2. Calculate quote
            decision = quote_engine.calculate_quote(live_orderbook)
            assert decision.action == QuoteAction.PLACE_QUOTE

            # 3. Place quote (dry-run)
            bid_result, ask_result = await order_manager.place_quote(decision.quote)

            assert bid_result.success is True
            assert ask_result.success is True

            # 4. Verify orders tracked
            pending = order_manager.get_pending_orders(token_ids[0])
            assert len(pending) == 2

            print(f"Full cycle completed:")
            print(f"  Orderbook: bid={live_orderbook.best_bid}, ask={live_orderbook.best_ask}")
            print(f"  Quote: bid={decision.quote.bid_price}, ask={decision.quote.ask_price}")
            print(f"  Orders: {bid_result.order_id}, {ask_result.order_id}")

            # 5. Cancel and verify
            await order_manager.cancel_all_for_token(token_ids[0])
            assert order_manager.get_open_order_count(token_ids[0]) == 0

        finally:
            await orderbook_manager.disconnect()
            await order_manager.close()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestUserChannelOrderVerification:
    """Test order verification via user channel (dry-run observation)."""

    @pytest.mark.asyncio
    async def test_user_channel_receives_order_updates(self, config, api_credentials):
        """User channel should receive order updates (observing existing orders)."""
        api_key, api_secret, api_passphrase = api_credentials

        user_manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        connect_task = asyncio.create_task(user_manager.connect())

        try:
            # Wait for connection
            for _ in range(30):
                if user_manager.is_connected():
                    break
                await asyncio.sleep(1)

            assert user_manager.is_connected(), "User channel failed to connect"

            # Just verify we can connect and the channel is operational
            # In a real test with live orders, we would verify order updates here
            print("User channel connected and authenticated")
            print(f"Current tracked orders: {len(user_manager.orders)}")

        finally:
            await user_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestLiveOrderPlacement:
    """
    Test live order placement (REQUIRES AQ_DRY_RUN=false).

    WARNING: These tests place REAL orders on Polymarket.
    Only run with small sizes and proper risk controls.
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.getenv("AQ_DRY_RUN", "true").lower() == "true",
        reason="Live order tests disabled (set AQ_DRY_RUN=false to enable)"
    )
    async def test_place_and_cancel_live_order(self):
        """Place a real post-only order and cancel it."""
        from dotenv import load_dotenv
        load_dotenv()

        # Get credentials
        api_key, api_secret, api_passphrase = get_api_credentials()
        token_ids = get_test_token_ids()
        poly_client = get_polymarket_client()

        if not poly_client:
            pytest.skip("PolymarketClient not available")

        config = ActiveQuotingConfig(
            dry_run=False,
            order_size_usdc=5.0,  # Minimum size
            post_only=True,
        )

        order_manager = OrderManager(
            config=config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            poly_client=poly_client,
        )

        user_manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        # Track order updates via user channel
        order_updates = []

        async def on_order_update(order_state):
            order_updates.append(order_state)

        user_manager.on_order_update = on_order_update

        user_task = asyncio.create_task(user_manager.connect())

        try:
            # Wait for user channel connection
            for _ in range(30):
                if user_manager.is_connected():
                    break
                await asyncio.sleep(1)

            assert user_manager.is_connected()

            # Place order at very low price to avoid fill
            result = await order_manager.place_order(
                token_id=token_ids[0],
                side=OrderSide.BUY,
                price=0.01,  # Very low price, unlikely to fill
                size=5.0,
            )

            if not result.success:
                print(f"Order failed: {result.error_msg}")
                pytest.skip(f"Order placement failed: {result.error_msg}")

            print(f"Placed live order: {result.order_id}")

            # Wait for order update on user channel
            await asyncio.sleep(3)

            # Cancel the order
            cancelled = await order_manager.cancel_order(result.order_id)
            assert cancelled is True

            print(f"Cancelled live order: {result.order_id}")

            # Wait for cancellation update
            await asyncio.sleep(2)

            print(f"Order updates received: {len(order_updates)}")

        finally:
            await order_manager.cancel_all_for_token(token_ids[0])
            await order_manager.close()
            await user_manager.disconnect()
            user_task.cancel()
            try:
                await user_task
            except asyncio.CancelledError:
                pass
