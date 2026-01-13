"""
Integration tests for Inventory Tracking (Phase 3).

Phase 3 Integration Gate:
- Verify inventory updates from user channel fills
- Detect real momentum on live market (if price moves during test)
- Fill one side, verify quote skew changes

Run with:
    POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_inventory_tracking.py -v
"""
import asyncio
import os
import pytest
from datetime import datetime, timedelta
from typing import Optional

from rebates.active_quoting.inventory_manager import InventoryManager
from rebates.active_quoting.momentum_detector import MomentumDetector
from rebates.active_quoting.quote_engine import QuoteEngine, QuoteAction
from rebates.active_quoting.orderbook_manager import OrderbookManager
from rebates.active_quoting.user_channel_manager import UserChannelManager
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import (
    OrderSide,
    OrderStatus,
    Fill,
    OrderbookState,
    OrderbookLevel,
)
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


@pytest.fixture
def config():
    """Provide config for integration tests."""
    return ActiveQuotingConfig(
        dry_run=True,
        order_size_usdc=5.0,
        ws_reconnect_delay_seconds=2.0,
        ws_max_reconnect_attempts=3,
        momentum_threshold_ticks=3,
        momentum_window_ms=500,
        cooldown_seconds=2.0,
        sweep_depth_threshold=0.5,
        inventory_skew_coefficient=0.1,
        max_position_per_market=100,
        max_liability_per_market_usdc=50.0,
    )


@pytest.fixture
def api_credentials():
    """Provide API credentials."""
    return get_api_credentials()


class TestInventoryUpdatesFromFills:
    """Test inventory updates from user channel fills."""

    def test_inventory_updates_from_simulated_fill(self, config):
        """Inventory should update correctly from simulated fills."""
        inventory_manager = InventoryManager(config)

        # Simulate buy fill
        buy_fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.01,
        )
        inventory_manager.update_from_fill(buy_fill)

        assert inventory_manager.get_inventory("token1") == 10.0
        assert inventory_manager.calculate_liability("token1") == pytest.approx(5.0)

        # Simulate sell fill
        sell_fill = Fill(
            order_id="order2",
            token_id="token1",
            side=OrderSide.SELL,
            price=0.55,
            size=5.0,
            fee=0.01,
        )
        inventory_manager.update_from_fill(sell_fill)

        assert inventory_manager.get_inventory("token1") == 5.0

    @pytest.mark.asyncio
    async def test_user_channel_fill_updates_inventory(self, config, api_credentials):
        """User channel fills should update inventory manager."""
        api_key, api_secret, api_passphrase = api_credentials

        inventory_manager = InventoryManager(config)
        fills_received = []

        async def on_fill(fill):
            fills_received.append(fill)
            inventory_manager.update_from_fill(fill)

        user_manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            on_fill=on_fill,
        )

        connect_task = asyncio.create_task(user_manager.connect())

        try:
            # Wait for connection
            for _ in range(30):
                if user_manager.is_connected():
                    break
                await asyncio.sleep(1)

            assert user_manager.is_connected(), "User channel failed to connect"

            # Simulate a fill event by manually triggering the callback
            # (In production, real fills come from the WebSocket)
            test_fill = Fill(
                order_id="test_order",
                token_id="test_token",
                side=OrderSide.BUY,
                price=0.50,
                size=15.0,
                fee=0.02,
            )
            await on_fill(test_fill)

            assert len(fills_received) == 1
            assert inventory_manager.get_inventory("test_token") == 15.0

            print(f"User channel connected, fill callback tested")
            print(f"  Inventory: {inventory_manager.get_inventory('test_token')}")
            print(f"  Liability: {inventory_manager.calculate_liability('test_token')}")

        finally:
            await user_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestMomentumDetectionLive:
    """Test momentum detection on live markets."""

    @pytest.mark.asyncio
    async def test_momentum_detector_with_live_trades(self, config, api_credentials):
        """Momentum detector should process live trade events."""
        token_ids = get_test_token_ids()

        momentum_detector = MomentumDetector(config)
        orderbook_manager = OrderbookManager(config)

        trade_prices = []
        momentum_events = []

        async def on_trade(token_id, price, tick_size):
            trade_prices.append(price)
            event = await momentum_detector.on_trade(token_id, price, tick_size)
            if event:
                momentum_events.append(event)

        book_received = asyncio.Event()
        trades_received = asyncio.Event()

        async def on_book_update(token_id, orderbook):
            if orderbook.bids and orderbook.asks:
                book_received.set()
            # Check for last trade price updates
            if orderbook.last_trade_price is not None:
                await on_trade(token_id, orderbook.last_trade_price, orderbook.tick_size)
                trades_received.set()

        orderbook_manager.on_book_update = on_book_update
        connect_task = asyncio.create_task(orderbook_manager.connect(token_ids[:1]))

        try:
            # Wait for orderbook
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            # Wait a bit for potential trade data
            try:
                await asyncio.wait_for(trades_received.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                print("No trades observed in 10 seconds (market may be quiet)")

            print(f"Trades observed: {len(trade_prices)}")
            print(f"Momentum events: {len(momentum_events)}")

            if trade_prices:
                print(f"  Price range: {min(trade_prices):.4f} - {max(trade_prices):.4f}")

            # Verify detector state is tracking correctly
            state = momentum_detector.get_state(token_ids[0])
            assert state is not None
            assert state.token_id == token_ids[0]

        finally:
            await orderbook_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_sweep_detection_with_orderbook_updates(self, config, api_credentials):
        """Sweep detection should work with orderbook updates."""
        token_ids = get_test_token_ids()

        momentum_detector = MomentumDetector(config)
        orderbook_manager = OrderbookManager(config)

        book_updates = []
        sweep_events = []

        book_received = asyncio.Event()

        async def on_book_update(token_id, orderbook):
            book_updates.append(orderbook)
            if orderbook.bids and orderbook.asks:
                book_received.set()
                # Check for sweeps
                event = await momentum_detector.on_orderbook_update(orderbook)
                if event:
                    sweep_events.append(event)

        orderbook_manager.on_book_update = on_book_update
        connect_task = asyncio.create_task(orderbook_manager.connect(token_ids[:1]))

        try:
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            # Wait for more updates to detect potential sweeps
            await asyncio.sleep(5)

            print(f"Book updates received: {len(book_updates)}")
            print(f"Sweep events detected: {len(sweep_events)}")

            if book_updates:
                last_book = book_updates[-1]
                print(f"  Last book: bid_depth={last_book.bid_depth():.1f}, ask_depth={last_book.ask_depth():.1f}")

        finally:
            await orderbook_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestQuoteSkewingFromInventory:
    """Test quote skewing based on inventory."""

    def test_quote_skew_changes_with_inventory(self, config):
        """Quotes should skew based on inventory position."""
        inventory_manager = InventoryManager(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)

        # Create test orderbook
        orderbook = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.49, size=100.0),
                OrderbookLevel(price=0.48, size=100.0),
            ],
            asks=[
                OrderbookLevel(price=0.51, size=100.0),
                OrderbookLevel(price=0.52, size=100.0),
            ],
            tick_size=0.01,
        )

        # Quote with no inventory
        decision_no_inv = quote_engine.calculate_quote_with_manager(orderbook)
        assert decision_no_inv.action == QuoteAction.PLACE_QUOTE
        base_bid = decision_no_inv.quote.bid_price
        base_ask = decision_no_inv.quote.ask_price

        print(f"Quote with no inventory: bid={base_bid}, ask={base_ask}")

        # Add inventory (buy fill)
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=20.0,  # 20 shares * 0.1 coef = 2 ticks skew
        )
        inventory_manager.update_from_fill(fill)

        # Quote with inventory
        decision_with_inv = quote_engine.calculate_quote_with_manager(orderbook)
        assert decision_with_inv.action == QuoteAction.PLACE_QUOTE
        skewed_bid = decision_with_inv.quote.bid_price
        skewed_ask = decision_with_inv.quote.ask_price

        print(f"Quote with 20 shares inventory: bid={skewed_bid}, ask={skewed_ask}")

        # Verify skew direction (long = lower prices)
        assert skewed_bid < base_bid
        assert skewed_ask < base_ask

    @pytest.mark.asyncio
    async def test_live_orderbook_quote_with_inventory(self, config, api_credentials):
        """Test quote calculation with live orderbook and inventory."""
        token_ids = get_test_token_ids()

        inventory_manager = InventoryManager(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)
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
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            assert live_orderbook is not None

            # Quote without inventory
            decision_no_inv = quote_engine.calculate_quote_with_manager(live_orderbook)
            assert decision_no_inv.action == QuoteAction.PLACE_QUOTE

            print(f"Live orderbook: bid={live_orderbook.best_bid}, ask={live_orderbook.best_ask}")
            print(f"Quote (no inventory): bid={decision_no_inv.quote.bid_price}, ask={decision_no_inv.quote.ask_price}")

            # Add inventory
            fill = Fill(
                order_id="test",
                token_id=token_ids[0],
                side=OrderSide.BUY,
                price=live_orderbook.mid_price(),
                size=30.0,  # 30 * 0.1 = 3 ticks
            )
            inventory_manager.update_from_fill(fill)

            # Quote with inventory
            decision_with_inv = quote_engine.calculate_quote_with_manager(live_orderbook)
            assert decision_with_inv.action == QuoteAction.PLACE_QUOTE

            print(f"Quote (30 shares): bid={decision_with_inv.quote.bid_price}, ask={decision_with_inv.quote.ask_price}")
            print(f"Inventory: {inventory_manager.get_inventory(token_ids[0])}")
            print(f"Skew factor: {inventory_manager.calculate_skew_factor(token_ids[0])}")

        finally:
            await orderbook_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestPositionLimitsEnforcement:
    """Test position limits are enforced."""

    def test_position_limits_block_orders(self, config):
        """Position limits should block orders when at limit."""
        inventory_manager = InventoryManager(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)

        # Create test orderbook
        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        # Set position at limit
        inventory_manager.set_position("token1", size=100, avg_entry_price=0.10)

        # Try to get buy quote - should be blocked
        price, reason = quote_engine.calculate_quote_for_side_with_manager(
            orderbook, OrderSide.BUY
        )

        assert price is None
        assert "Position" in reason

        # Sell should still work
        price, reason = quote_engine.calculate_quote_for_side_with_manager(
            orderbook, OrderSide.SELL
        )

        assert price is not None
        print(f"At position limit: sell price={price}")

    def test_adjusted_order_sizes(self, config):
        """Order sizes should be adjusted based on limits."""
        inventory_manager = InventoryManager(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)

        # Position near limit
        inventory_manager.set_position("token1", size=90, avg_entry_price=0.10)

        buy_size, sell_size = quote_engine.get_inventory_adjusted_sizes("token1", 20.0)

        # Can only buy 10 more (100 - 90)
        assert buy_size == 10
        # Can sell up to position
        assert sell_size == 20  # min(20, 90) = 20

        print(f"Near limit: buy_size={buy_size}, sell_size={sell_size}")


class TestIntegratedInventoryMomentum:
    """Test integrated inventory and momentum management."""

    def test_momentum_cooldown_blocks_quotes(self, config):
        """Momentum cooldown should block quote calculation."""
        inventory_manager = InventoryManager(config)
        momentum_detector = MomentumDetector(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)

        orderbook = OrderbookState(
            token_id="token1",
            bids=[OrderbookLevel(price=0.49, size=100.0)],
            asks=[OrderbookLevel(price=0.51, size=100.0)],
            tick_size=0.01,
        )

        # Force cooldown
        momentum_detector.force_cooldown("token1", seconds=5.0)
        momentum_state = momentum_detector.get_state("token1")

        # Quote calculation should return CANCEL_ALL during cooldown
        decision = quote_engine.calculate_quote(
            orderbook,
            inventory=inventory_manager.get_inventory("token1"),
            momentum_state=momentum_state,
        )

        assert decision.action == QuoteAction.CANCEL_ALL
        assert "cooldown" in decision.reason.lower()

        print(f"In cooldown: action={decision.action}, reason={decision.reason}")

    @pytest.mark.asyncio
    async def test_full_quote_cycle_with_inventory_momentum(self, config, api_credentials):
        """Test full quote cycle with inventory manager and momentum detector."""
        token_ids = get_test_token_ids()

        inventory_manager = InventoryManager(config)
        momentum_detector = MomentumDetector(config)
        quote_engine = QuoteEngine(config, inventory_manager=inventory_manager)
        orderbook_manager = OrderbookManager(config)

        book_received = asyncio.Event()
        live_orderbook = None

        async def on_book_update(token_id, orderbook):
            nonlocal live_orderbook
            if orderbook.bids and orderbook.asks:
                live_orderbook = orderbook
                book_received.set()

                # Check for momentum on trade updates
                if orderbook.last_trade_price:
                    await momentum_detector.on_trade(
                        token_id, orderbook.last_trade_price, orderbook.tick_size
                    )

        orderbook_manager.on_book_update = on_book_update
        connect_task = asyncio.create_task(orderbook_manager.connect(token_ids[:1]))

        try:
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            token_id = token_ids[0]
            momentum_state = momentum_detector.get_state(token_id)

            # Get quote with all context
            decision = quote_engine.calculate_quote(
                live_orderbook,
                inventory=inventory_manager.get_inventory(token_id),
                momentum_state=momentum_state,
            )

            print(f"Full cycle quote decision:")
            print(f"  Action: {decision.action}")
            print(f"  Reason: {decision.reason}")
            if decision.quote:
                print(f"  Quote: bid={decision.quote.bid_price}, ask={decision.quote.ask_price}")

            # Simulate a fill
            fill = Fill(
                order_id="test",
                token_id=token_id,
                side=OrderSide.BUY,
                price=live_orderbook.mid_price(),
                size=15.0,
            )
            inventory_manager.update_from_fill(fill)

            # Get updated quote with new inventory
            decision2 = quote_engine.calculate_quote(
                live_orderbook,
                inventory=inventory_manager.get_inventory(token_id),
                momentum_state=momentum_state,
            )

            print(f"After fill:")
            print(f"  Inventory: {inventory_manager.get_inventory(token_id)}")
            if decision2.quote:
                print(f"  Quote: bid={decision2.quote.bid_price}, ask={decision2.quote.ask_price}")

            # Verify skew changed
            if decision.quote and decision2.quote:
                assert decision2.quote.bid_price < decision.quote.bid_price or \
                       decision2.quote.ask_price < decision.quote.ask_price

        finally:
            await orderbook_manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
