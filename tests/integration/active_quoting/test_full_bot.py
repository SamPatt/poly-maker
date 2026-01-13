"""
Integration tests for ActiveQuotingBot - Full bot testing.

Tests:
- Bot starts and connects to WebSockets
- Bot runs on 1 market for simulated quote cycle
- Bot handles market WebSocket reconnect gracefully
- Bot handles user WebSocket reconnect gracefully
- Markout tracking records fills correctly
- Multi-market coordination (3 markets)

Run with: POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_full_bot.py -v

NOTE: These integration tests mock the WebSocket connections to test the bot's
orchestration logic without requiring real API credentials. For full end-to-end
testing with real WebSockets, use valid API credentials.
"""
import asyncio
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from rebates.active_quoting.bot import ActiveQuotingBot
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import (
    OrderbookState,
    OrderbookLevel,
    Quote,
    Fill,
    OrderSide,
    MomentumState,
    Position,
    MarketState,
)
from rebates.active_quoting.risk_manager import CircuitBreakerState


# Skip all tests if POLY_TEST_INTEGRATION is not set
pytestmark = pytest.mark.skipif(
    os.getenv("POLY_TEST_INTEGRATION", "").lower() != "true",
    reason="Integration tests require POLY_TEST_INTEGRATION=true",
)


@pytest.fixture
def config():
    """Create test configuration for integration tests."""
    return ActiveQuotingConfig(
        order_size_usdc=5.0,  # Minimum order size
        min_refresh_interval_ms=500,
        global_refresh_cap_per_sec=10,
        dry_run=True,  # Always dry run for integration tests
        ws_max_reconnect_attempts=3,
        ws_reconnect_delay_seconds=1,
    )


@pytest.fixture
def api_credentials():
    """Get API credentials from environment."""
    return {
        "api_key": os.getenv("POLY_API_KEY", "test_key"),
        "api_secret": os.getenv("POLY_API_SECRET", "test_secret"),
        "api_passphrase": os.getenv("POLY_PASSPHRASE", "test_passphrase"),
    }


@pytest.fixture
def sample_token_id():
    """
    Get a sample token ID for testing.

    Uses a real token ID if available from env, otherwise uses a mock.
    """
    return os.getenv("TEST_TOKEN_ID", "sample_token_id_for_testing")


@pytest.fixture
def sample_token_ids():
    """Get sample token IDs for multi-market testing."""
    env_tokens = os.getenv("TEST_TOKEN_IDS", "")
    if env_tokens:
        return env_tokens.split(",")
    return ["token_1", "token_2", "token_3"]


@pytest.fixture
def valid_orderbook():
    """Create a valid orderbook for testing."""
    return OrderbookState(
        token_id="test_token",
        bids=[
            OrderbookLevel(price=0.49, size=100),
            OrderbookLevel(price=0.48, size=200),
        ],
        asks=[
            OrderbookLevel(price=0.51, size=100),
            OrderbookLevel(price=0.52, size=200),
        ],
        tick_size=0.01,
    )


def create_mocked_bot(config, api_credentials):
    """Create a bot with mocked WebSocket managers for integration testing."""
    bot = ActiveQuotingBot(
        config=config,
        api_key=api_credentials["api_key"],
        api_secret=api_credentials["api_secret"],
        api_passphrase=api_credentials["api_passphrase"],
    )

    # Mock WebSocket managers to avoid real connections
    bot.orderbook_manager = MagicMock()
    bot.orderbook_manager.connect = AsyncMock()
    bot.orderbook_manager.disconnect = AsyncMock()
    bot.orderbook_manager.is_connected = MagicMock(return_value=True)
    bot.orderbook_manager.get_orderbook = MagicMock(return_value=None)

    bot.user_channel_manager = MagicMock()
    bot.user_channel_manager.connect = AsyncMock()
    bot.user_channel_manager.disconnect = AsyncMock()
    bot.user_channel_manager.is_connected = MagicMock(return_value=True)

    # Mock order manager
    bot.order_manager.close = AsyncMock()
    bot.order_manager.cancel_all = AsyncMock(return_value=0)
    bot.order_manager.cancel_all_for_token = AsyncMock(return_value=0)

    return bot


# --- WebSocket Connection Tests ---

class TestWebSocketConnectivity:
    """Integration tests for WebSocket connectivity."""

    @pytest.mark.asyncio
    async def test_bot_starts_and_connects(self, config, api_credentials, sample_token_id):
        """Test that bot starts and attempts to connect to WebSockets."""
        bot = create_mocked_bot(config, api_credentials)

        # Start bot with timeout
        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            assert bot._running is True
            assert sample_token_id in bot._markets
            assert sample_token_id in bot._active_tokens

        finally:
            await bot.stop()

        assert bot._running is False

    @pytest.mark.asyncio
    async def test_bot_connects_to_market_websocket(self, config, api_credentials, sample_token_id):
        """Test that bot connects to market WebSocket channel."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Verify WebSocket connect was called
            bot.orderbook_manager.connect.assert_called_once_with([sample_token_id])

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_connects_to_user_websocket(self, config, api_credentials, sample_token_id):
        """Test that bot connects to user WebSocket channel."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Verify user channel connect was called
            bot.user_channel_manager.connect.assert_called_once()

        finally:
            await bot.stop()


# --- Single Market Quote Cycle Tests ---

class TestSingleMarketQuoteCycle:
    """Integration tests for single market quote cycling."""

    @pytest.mark.asyncio
    async def test_bot_runs_quote_cycle(self, config, api_credentials, sample_token_id):
        """Test that bot runs through a quote cycle for one market."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Let main loop run for a few iterations
            await asyncio.sleep(1.0)

            # Check that bot is still running
            assert bot.is_running()

            # Get status
            status = bot.get_status()
            assert status["running"] is True
            assert status["active_markets"] == 1

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_handles_simulated_fills(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that bot handles simulated fill events correctly."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook for mid price calculation
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Simulate a fill
            fill = Fill(
                order_id="test_order",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,  # Rebate
                trade_id="test_trade_1",
            )

            await bot._on_fill(fill)

            # Check inventory was updated
            position = bot.inventory_manager.get_position(sample_token_id)
            assert position.size == 10.0

            # Check analytics recorded the fill
            record = bot.fill_analytics.get_fill_record("test_trade_1")
            assert record is not None

        finally:
            await bot.stop()


# --- WebSocket Reconnect Tests ---

class TestWebSocketReconnect:
    """Integration tests for WebSocket reconnection handling."""

    @pytest.mark.asyncio
    async def test_bot_handles_market_ws_disconnect(self, config, api_credentials, sample_token_id):
        """Test that bot handles market WebSocket disconnect gracefully."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Simulate market WS disconnect
            await bot._on_market_ws_disconnect()

            # Bot should still be running
            assert bot.is_running()

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_handles_user_ws_disconnect(self, config, api_credentials, sample_token_id):
        """Test that bot handles user WebSocket disconnect gracefully."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Simulate user WS disconnect
            await bot._on_user_ws_disconnect()

            # Bot should still be running (trying to reconnect)
            assert bot.is_running()

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_continues_after_reconnect(self, config, api_credentials, sample_token_id):
        """Test that bot continues operating after WebSocket reconnect."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Simulate disconnect and reconnect cycle
            await bot._on_market_ws_disconnect()

            # Wait a bit
            await asyncio.sleep(0.5)

            # Bot should still be running
            assert bot.is_running()

            # Get status to verify
            status = bot.get_status()
            assert status["running"] is True

        finally:
            await bot.stop()


# --- Markout Tracking Tests ---

class TestMarkoutTracking:
    """Integration tests for markout tracking and analytics."""

    @pytest.mark.asyncio
    async def test_markout_tracking_records_fills(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that markout tracking correctly records fills."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record multiple fills
            for i in range(3):
                fill = Fill(
                    order_id=f"order_{i}",
                    token_id=sample_token_id,
                    side=OrderSide.BUY if i % 2 == 0 else OrderSide.SELL,
                    price=0.50,
                    size=10.0,
                    fee=-0.01,
                    trade_id=f"trade_{i}",
                )
                await bot._on_fill(fill)

            # Check analytics
            summary = bot.fill_analytics.get_summary()
            assert summary["total_fills"] == 3
            assert summary["total_volume"] == 30.0

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_markout_captures_after_delay(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that markout captures happen after the specified delay."""
        bot = create_mocked_bot(config, api_credentials)

        # Use shorter horizons for testing
        bot.fill_analytics.horizons = [1, 2]

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record a fill with old timestamp so markouts are due
            fill = Fill(
                order_id="order_1",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,
                timestamp=datetime.utcnow() - timedelta(seconds=5),
                trade_id="trade_markout_test",
            )
            await bot._on_fill(fill)

            # Process any due markouts
            def get_mid_price(token_id):
                return 0.51  # Price moved up

            captured = bot.fill_analytics.process_markout_captures(get_mid_price)

            # Should have captured some markouts
            assert len(captured) > 0

        finally:
            await bot.stop()


# --- Multi-Market Coordination Tests ---

class TestMultiMarketCoordination:
    """Integration tests for multi-market support."""

    @pytest.mark.asyncio
    async def test_bot_starts_with_multiple_markets(self, config, api_credentials, sample_token_ids):
        """Test that bot can start with multiple markets."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start(sample_token_ids), timeout=5.0)

            assert bot.is_running()
            assert len(bot._markets) == len(sample_token_ids)
            assert len(bot._active_tokens) == len(sample_token_ids)

            for token_id in sample_token_ids:
                assert token_id in bot._markets

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_fills_tracked_per_market(self, config, api_credentials, sample_token_ids, valid_orderbook):
        """Test that fills are tracked separately per market."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start(sample_token_ids), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record fills for different markets
            fills_per_market = {}
            for i, token_id in enumerate(sample_token_ids):
                size = 10.0 * (i + 1)
                fills_per_market[token_id] = size

                fill = Fill(
                    order_id=f"order_{token_id}",
                    token_id=token_id,
                    side=OrderSide.BUY,
                    price=0.50,
                    size=size,
                    fee=-0.01,
                    trade_id=f"trade_{token_id}",
                )
                await bot._on_fill(fill)

            # Verify per-market tracking
            for token_id, expected_size in fills_per_market.items():
                position = bot.inventory_manager.get_position(token_id)
                assert position.size == expected_size

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_shared_risk_limits_across_markets(self, config, api_credentials, sample_token_ids, valid_orderbook):
        """Test that risk limits are shared across markets."""
        # Set low liability limits
        config.max_total_liability_usdc = 50.0

        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start(sample_token_ids), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record fills that approach liability limit
            for token_id in sample_token_ids:
                fill = Fill(
                    order_id=f"order_{token_id}",
                    token_id=token_id,
                    side=OrderSide.BUY,
                    price=0.50,
                    size=30.0,  # Each fill adds ~15 USDC liability
                    fee=-0.01,
                    trade_id=f"trade_{token_id}",
                )
                await bot._on_fill(fill)

            # Check total liability
            total_liability = bot.inventory_manager.calculate_total_liability()
            assert total_liability > 0

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_three_market_coordination(self, config, api_credentials):
        """Test coordination with exactly 3 markets."""
        tokens = ["market_a", "market_b", "market_c"]

        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start(tokens), timeout=5.0)

            # Verify all markets initialized
            assert len(bot._markets) == 3

            # Verify status reports all markets
            status = bot.get_status()
            assert status["active_markets"] == 3

            # Let it run briefly
            await asyncio.sleep(0.5)

            # Should still be running
            assert bot.is_running()

        finally:
            await bot.stop()


# --- Graceful Shutdown Tests ---

class TestGracefulShutdown:
    """Integration tests for graceful shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_all_orders(self, config, api_credentials, sample_token_id):
        """Test that shutdown cancels all outstanding orders."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Let it run
            await asyncio.sleep(0.5)

        finally:
            await bot.stop()

        # Verify shutdown completed
        assert bot.is_running() is False
        bot.order_manager.cancel_all.assert_called()

    @pytest.mark.asyncio
    async def test_shutdown_with_pending_fills(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that shutdown handles pending fill analytics correctly."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record a fill
            fill = Fill(
                order_id="order_1",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,
                trade_id="shutdown_test_trade",
            )
            await bot._on_fill(fill)

        finally:
            await bot.stop()

        # Should shutdown cleanly even with pending markouts
        assert bot.is_running() is False


# --- Status and Monitoring Tests ---

class TestStatusMonitoring:
    """Integration tests for status and monitoring."""

    @pytest.mark.asyncio
    async def test_status_reports_connection_state(self, config, api_credentials, sample_token_id):
        """Test that status correctly reports connection states."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            status = bot.get_status()

            assert "running" in status
            assert "market_ws_connected" in status
            assert "user_ws_connected" in status
            assert "circuit_breaker_state" in status
            assert "open_orders" in status
            assert "positions" in status
            assert "risk" in status
            assert "analytics" in status

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_analytics_summary_available(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that analytics summary is available during operation."""
        bot = create_mocked_bot(config, api_credentials)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record some fills
            for i in range(5):
                fill = Fill(
                    order_id=f"order_{i}",
                    token_id=sample_token_id,
                    side=OrderSide.BUY,
                    price=0.50,
                    size=10.0,
                    fee=-0.01,
                    trade_id=f"analytics_test_{i}",
                )
                await bot._on_fill(fill)

            # Get analytics from status
            status = bot.get_status()
            analytics = status["analytics"]

            assert analytics["total_fills"] == 5
            assert analytics["total_volume"] == 50.0

        finally:
            await bot.stop()


# --- Persistence Integration Tests (Phase 6) ---

class TestPersistenceIntegration:
    """Integration tests for database persistence."""

    @pytest.mark.asyncio
    async def test_bot_starts_with_persistence_disabled(self, config, api_credentials, sample_token_id):
        """Test that bot starts correctly with persistence disabled."""
        bot = ActiveQuotingBot(
            config=config,
            api_key=api_credentials["api_key"],
            api_secret=api_credentials["api_secret"],
            api_passphrase=api_credentials["api_passphrase"],
            enable_persistence=False,
            enable_alerts=False,
        )

        # Mock WebSocket managers
        bot.orderbook_manager = MagicMock()
        bot.orderbook_manager.connect = AsyncMock()
        bot.orderbook_manager.disconnect = AsyncMock()
        bot.orderbook_manager.is_connected = MagicMock(return_value=True)
        bot.orderbook_manager.get_orderbook = MagicMock(return_value=None)

        bot.user_channel_manager = MagicMock()
        bot.user_channel_manager.connect = AsyncMock()
        bot.user_channel_manager.disconnect = AsyncMock()
        bot.user_channel_manager.is_connected = MagicMock(return_value=True)

        bot.order_manager.close = AsyncMock()
        bot.order_manager.cancel_all = AsyncMock(return_value=0)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Persistence should be disabled
            assert bot.persistence.is_enabled is False

            # Session ID should still be generated (for local tracking)
            assert bot.persistence.session_id is not None

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_persists_fills(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that bot persists fills when persistence is mocked."""
        bot = create_mocked_bot(config, api_credentials)

        # Mock persistence
        bot.persistence.save_fill_record = MagicMock(return_value=True)
        bot.persistence.save_position = MagicMock(return_value=True)
        bot.persistence.is_enabled = True

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record a fill
            fill = Fill(
                order_id="order_persist_test",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,
                trade_id="trade_persist_test",
            )
            await bot._on_fill(fill)

            # Verify persistence was called
            bot.persistence.save_fill_record.assert_called_once()
            bot.persistence.save_position.assert_called_once()

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_starts_with_market_names(self, config, api_credentials, sample_token_id):
        """Test that bot accepts market names mapping."""
        bot = create_mocked_bot(config, api_credentials)

        market_names = {sample_token_id: "BTC Up or Down"}

        try:
            await asyncio.wait_for(
                bot.start([sample_token_id], market_names=market_names),
                timeout=5.0
            )

            # Market names should be stored
            assert sample_token_id in bot._market_names
            assert bot._market_names[sample_token_id] == "BTC Up or Down"

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_session_ends_on_shutdown(self, config, api_credentials, sample_token_id):
        """Test that session is properly ended on shutdown."""
        bot = create_mocked_bot(config, api_credentials)

        # Mock persistence
        bot.persistence.end_session = MagicMock(return_value=True)
        bot.persistence.is_enabled = True
        bot.persistence._session_id = "test_session_123"

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)
            await asyncio.sleep(0.5)

        finally:
            await bot.stop()

        # Verify end_session was called
        bot.persistence.end_session.assert_called_once()
        call_kwargs = bot.persistence.end_session.call_args[1]
        assert call_kwargs["status"] == "STOPPED"


# --- Alerts Integration Tests (Phase 6) ---

class TestAlertsIntegration:
    """Integration tests for Telegram alerts."""

    @pytest.mark.asyncio
    async def test_bot_starts_with_alerts_disabled(self, config, api_credentials, sample_token_id):
        """Test that bot starts correctly with alerts disabled."""
        bot = ActiveQuotingBot(
            config=config,
            api_key=api_credentials["api_key"],
            api_secret=api_credentials["api_secret"],
            api_passphrase=api_credentials["api_passphrase"],
            enable_persistence=False,
            enable_alerts=False,
        )

        # Mock WebSocket managers
        bot.orderbook_manager = MagicMock()
        bot.orderbook_manager.connect = AsyncMock()
        bot.orderbook_manager.disconnect = AsyncMock()
        bot.orderbook_manager.is_connected = MagicMock(return_value=True)
        bot.orderbook_manager.get_orderbook = MagicMock(return_value=None)

        bot.user_channel_manager = MagicMock()
        bot.user_channel_manager.connect = AsyncMock()
        bot.user_channel_manager.disconnect = AsyncMock()
        bot.user_channel_manager.is_connected = MagicMock(return_value=True)

        bot.order_manager.close = AsyncMock()
        bot.order_manager.cancel_all = AsyncMock(return_value=0)

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Alerts should be disabled
            assert bot._enable_alerts is False

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    @patch("rebates.active_quoting.bot.send_active_quoting_fill_alert")
    async def test_fill_triggers_alert(self, mock_alert, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that fills trigger alert when alerts enabled."""
        bot = create_mocked_bot(config, api_credentials)
        bot._enable_alerts = True

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record a fill
            fill = Fill(
                order_id="order_alert_test",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,
                trade_id="trade_alert_test",
            )
            await bot._on_fill(fill)

            # Verify alert was called
            mock_alert.assert_called_once()

        finally:
            bot._enable_alerts = False  # Prevent shutdown alert
            await bot.stop()

    @pytest.mark.asyncio
    @patch("rebates.active_quoting.bot.send_active_quoting_circuit_breaker_alert")
    async def test_circuit_breaker_triggers_alert(self, mock_alert, config, api_credentials, sample_token_id):
        """Test that circuit breaker state change triggers alert."""
        bot = create_mocked_bot(config, api_credentials)
        bot._enable_alerts = True

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Simulate circuit breaker state change
            await bot._on_circuit_breaker_state_change(
                old_state=CircuitBreakerState.NORMAL,
                new_state=CircuitBreakerState.WARNING,
                reason="Test trigger",
            )

            # Verify alert was called
            mock_alert.assert_called_once()

        finally:
            bot._enable_alerts = False
            await bot.stop()


# --- Position Recovery Tests (Phase 6) ---

class TestPositionRecovery:
    """Integration tests for position recovery on restart."""

    @pytest.mark.asyncio
    @patch("rebates.active_quoting.persistence.ActiveQuotingPersistence.load_positions")
    async def test_positions_loaded_on_startup(self, mock_load, config, api_credentials, sample_token_id):
        """Test that positions are loaded from database on startup."""
        # Mock saved positions
        mock_load.return_value = {
            sample_token_id: Position(
                token_id=sample_token_id,
                size=50.0,
                avg_entry_price=0.45,
                realized_pnl=2.50,
            )
        }

        bot = create_mocked_bot(config, api_credentials)
        bot.persistence._db_available = True
        bot.persistence.config.enabled = True
        bot.persistence.config.save_positions = True

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Position should be restored
            position = bot.inventory_manager.get_position(sample_token_id)
            assert position.size == 50.0
            assert position.avg_entry_price == 0.45

        finally:
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_survives_db_failure(self, config, api_credentials, sample_token_id, valid_orderbook):
        """Test that bot continues operating even if DB calls fail."""
        bot = create_mocked_bot(config, api_credentials)

        # Mock persistence to fail
        bot.persistence.save_fill_record = MagicMock(return_value=False)
        bot.persistence.save_position = MagicMock(side_effect=Exception("DB error"))
        bot.persistence.is_enabled = True

        try:
            await asyncio.wait_for(bot.start([sample_token_id]), timeout=5.0)

            # Mock orderbook
            bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

            # Record a fill - should not raise
            fill = Fill(
                order_id="order_db_fail_test",
                token_id=sample_token_id,
                side=OrderSide.BUY,
                price=0.50,
                size=10.0,
                fee=-0.01,
                trade_id="trade_db_fail_test",
            )
            await bot._on_fill(fill)

            # Bot should still be running
            assert bot.is_running()

            # Position should still be tracked in memory
            position = bot.inventory_manager.get_position(sample_token_id)
            assert position.size == 10.0

        finally:
            await bot.stop()
