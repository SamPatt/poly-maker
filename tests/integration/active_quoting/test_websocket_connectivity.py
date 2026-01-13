"""
Integration tests for WebSocket connectivity.

Phase 1 Integration Gate:
- Market WebSocket connects and receives book updates
- User WebSocket connects and authenticates
- Both stable for 5 minutes

Run with:
    POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_websocket_connectivity.py -v
"""
import asyncio
import os
import pytest
from datetime import datetime, timedelta

from rebates.active_quoting.orderbook_manager import OrderbookManager
from rebates.active_quoting.user_channel_manager import UserChannelManager
from rebates.active_quoting.config import ActiveQuotingConfig
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
    for market in markets[:3]:  # Get up to 3 markets
        # Each market has clobTokenIds with YES and NO tokens
        clob_token_ids = market.get("clobTokenIds", [])
        if clob_token_ids:
            # clobTokenIds may be a JSON string or a list
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json_module.loads(clob_token_ids)
                except json_module.JSONDecodeError:
                    continue
            if isinstance(clob_token_ids, list):
                token_ids.extend(clob_token_ids)

    if not token_ids:
        # Fallback: use a known active token if no upcoming markets
        # These are BTC Up/Down tokens that should have activity
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

    # Derive API credentials from private key (same as PolymarketClient)
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
        ws_reconnect_delay_seconds=2.0,
        ws_max_reconnect_attempts=3,
    )


class TestMarketWebSocketConnectivity:
    """Test market WebSocket connectivity."""

    @pytest.mark.asyncio
    async def test_market_ws_connects_and_receives_book(self, config):
        """Market WebSocket connects and receives book events."""
        token_ids = get_test_token_ids()

        book_received = asyncio.Event()
        received_tokens = set()

        async def on_book_update(token_id, orderbook):
            received_tokens.add(token_id)
            if orderbook.bids or orderbook.asks:
                book_received.set()

        manager = OrderbookManager(config, on_book_update=on_book_update)

        # Start connection in background
        connect_task = asyncio.create_task(manager.connect(token_ids))

        try:
            # Wait for book update (max 30 seconds)
            await asyncio.wait_for(book_received.wait(), timeout=30.0)

            assert manager.is_connected()
            assert len(received_tokens) > 0

            # Verify we have orderbook data
            for token_id in received_tokens:
                book = manager.get_orderbook(token_id)
                assert book is not None, f"No orderbook for {token_id}"

            print(f"Received book updates for {len(received_tokens)} tokens")

        finally:
            await manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_market_ws_receives_price_updates(self, config):
        """Market WebSocket receives ongoing price updates."""
        token_ids = get_test_token_ids()

        update_count = 0
        updates_received = asyncio.Event()

        async def on_book_update(token_id, orderbook):
            nonlocal update_count
            update_count += 1
            if update_count >= 5:
                updates_received.set()

        manager = OrderbookManager(config, on_book_update=on_book_update)
        connect_task = asyncio.create_task(manager.connect(token_ids))

        try:
            # Wait for multiple updates (max 60 seconds)
            await asyncio.wait_for(updates_received.wait(), timeout=60.0)
            assert update_count >= 5
            print(f"Received {update_count} book updates")

        finally:
            await manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestUserWebSocketConnectivity:
    """Test user WebSocket connectivity."""

    @pytest.mark.asyncio
    async def test_user_ws_connects_and_authenticates(self, config):
        """User WebSocket connects with credentials."""
        api_key, api_secret, api_passphrase = get_api_credentials()

        connected = asyncio.Event()

        manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        connect_task = asyncio.create_task(manager.connect())

        try:
            # Wait for connection and authentication
            for _ in range(30):  # Max 30 seconds
                if manager.is_connected():
                    connected.set()
                    break
                await asyncio.sleep(1)

            assert manager.is_connected(), "User WebSocket failed to authenticate"
            print("User WebSocket connected and authenticated")

        finally:
            await manager.disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass


class TestBothWebSocketsStability:
    """Test both WebSockets running together."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_both_ws_stable_for_5_minutes(self, config):
        """Both WebSockets stay connected for 5 minutes."""
        token_ids = get_test_token_ids()
        api_key, api_secret, api_passphrase = get_api_credentials()

        # Track disconnects
        market_disconnects = 0
        user_disconnects = 0
        market_updates = 0

        async def on_market_disconnect():
            nonlocal market_disconnects
            market_disconnects += 1

        async def on_user_disconnect():
            nonlocal user_disconnects
            user_disconnects += 1

        async def on_book_update(token_id, orderbook):
            nonlocal market_updates
            market_updates += 1

        market_manager = OrderbookManager(
            config,
            on_book_update=on_book_update,
            on_disconnect=on_market_disconnect,
        )
        user_manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            on_disconnect=on_user_disconnect,
        )

        market_task = asyncio.create_task(market_manager.connect(token_ids))
        user_task = asyncio.create_task(user_manager.connect())

        try:
            # Wait for both to connect
            for _ in range(30):
                if market_manager.is_connected() and user_manager.is_connected():
                    break
                await asyncio.sleep(1)

            assert market_manager.is_connected(), "Market WS did not connect"
            assert user_manager.is_connected(), "User WS did not connect"

            print("Both WebSockets connected. Running stability test for 5 minutes...")

            # Run for 5 minutes (300 seconds)
            # For quicker tests during development, can reduce this
            test_duration = 300  # 5 minutes
            start_time = datetime.utcnow()
            check_interval = 10  # Check every 10 seconds

            while (datetime.utcnow() - start_time).total_seconds() < test_duration:
                await asyncio.sleep(check_interval)

                # Check connections are still alive
                market_connected = market_manager.is_connected()
                user_connected = user_manager.is_connected()

                elapsed = (datetime.utcnow() - start_time).total_seconds()
                print(
                    f"  {elapsed:.0f}s: market={market_connected}, "
                    f"user={user_connected}, updates={market_updates}"
                )

                if not market_connected or not user_connected:
                    # Allow for reconnection
                    await asyncio.sleep(10)
                    if not market_manager.is_connected():
                        pytest.fail(f"Market WS disconnected and did not reconnect")
                    if not user_manager.is_connected():
                        pytest.fail(f"User WS disconnected and did not reconnect")

            # Final checks
            assert market_manager.is_connected(), "Market WS not connected at end"
            assert user_manager.is_connected(), "User WS not connected at end"
            assert market_updates > 0, "No market updates received"

            print(f"\nStability test passed:")
            print(f"  Duration: {test_duration} seconds")
            print(f"  Market updates: {market_updates}")
            print(f"  Market disconnects: {market_disconnects}")
            print(f"  User disconnects: {user_disconnects}")

        finally:
            await market_manager.disconnect()
            await user_manager.disconnect()
            market_task.cancel()
            user_task.cancel()
            try:
                await market_task
            except asyncio.CancelledError:
                pass
            try:
                await user_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_both_ws_stable_for_30_seconds(self, config):
        """Quick stability test - both WebSockets stable for 30 seconds."""
        token_ids = get_test_token_ids()
        api_key, api_secret, api_passphrase = get_api_credentials()

        market_updates = 0

        async def on_book_update(token_id, orderbook):
            nonlocal market_updates
            market_updates += 1

        market_manager = OrderbookManager(config, on_book_update=on_book_update)
        user_manager = UserChannelManager(
            config,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        market_task = asyncio.create_task(market_manager.connect(token_ids))
        user_task = asyncio.create_task(user_manager.connect())

        try:
            # Wait for connection
            for _ in range(30):
                if market_manager.is_connected() and user_manager.is_connected():
                    break
                await asyncio.sleep(1)

            assert market_manager.is_connected()
            assert user_manager.is_connected()

            # Run for 30 seconds
            await asyncio.sleep(30)

            assert market_manager.is_connected()
            assert user_manager.is_connected()
            assert market_updates > 0

            print(f"30-second test passed: {market_updates} updates received")

        finally:
            await market_manager.disconnect()
            await user_manager.disconnect()
            market_task.cancel()
            user_task.cancel()
            try:
                await market_task
            except asyncio.CancelledError:
                pass
            try:
                await user_task
            except asyncio.CancelledError:
                pass
