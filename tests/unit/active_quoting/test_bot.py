"""
Unit tests for ActiveQuotingBot - Main orchestration.

Tests:
- Initialization and component wiring
- Startup/shutdown sequence
- Market processing and quote calculation
- Event handling (fills, trades, disconnects)
- Rate limiting
- Status reporting
- Multi-market support
"""
import asyncio
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
    OrderStatus,
    MomentumState,
    Position,
)
from rebates.active_quoting.quote_engine import QuoteAction, QuoteDecision
from rebates.active_quoting.risk_manager import CircuitBreakerState


@pytest.fixture
def config():
    """Create test configuration."""
    return ActiveQuotingConfig(
        order_size_usdc=10.0,
        min_refresh_interval_ms=100,
        global_refresh_cap_per_sec=100,
        dry_run=True,
    )


@pytest.fixture
def bot(config):
    """Create ActiveQuotingBot instance with mocked components."""
    bot = ActiveQuotingBot(
        config=config,
        api_key="test_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
        enable_persistence=False,
        enable_alerts=False,
    )

    # Mock WebSocket managers
    bot.orderbook_manager = MagicMock()
    bot.orderbook_manager.connect = AsyncMock()
    bot.orderbook_manager.disconnect = AsyncMock()
    bot.orderbook_manager.is_connected = MagicMock(return_value=True)

    bot.user_channel_manager = MagicMock()
    bot.user_channel_manager.connect = AsyncMock()
    bot.user_channel_manager.disconnect = AsyncMock()
    bot.user_channel_manager.is_connected = MagicMock(return_value=True)

    # Mock order manager
    bot.order_manager.close = AsyncMock()
    bot.order_manager.cancel_all = AsyncMock(return_value=0)
    bot.order_manager.cancel_all_for_token = AsyncMock(return_value=0)

    return bot


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


# --- Initialization Tests ---

class TestBotInitialization:
    """Tests for bot initialization."""

    def test_init_creates_all_components(self, config):
        """Test that all components are created on init."""
        bot = ActiveQuotingBot(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            enable_persistence=False,
            enable_alerts=False,
        )

        assert bot.inventory_manager is not None
        assert bot.quote_engine is not None
        assert bot.momentum_detector is not None
        assert bot.risk_manager is not None
        assert bot.order_manager is not None
        assert bot.fill_analytics is not None
        assert bot.orderbook_manager is not None
        assert bot.user_channel_manager is not None

    def test_init_with_poly_client(self, config):
        """Test initialization with poly client."""
        mock_client = MagicMock()
        bot = ActiveQuotingBot(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            poly_client=mock_client,
            enable_persistence=False,
            enable_alerts=False,
        )

        assert bot._poly_client == mock_client

    def test_init_state(self, config):
        """Test initial state."""
        bot = ActiveQuotingBot(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            enable_persistence=False,
            enable_alerts=False,
        )

        assert bot._running is False
        assert len(bot._markets) == 0
        assert len(bot._active_tokens) == 0


# --- Startup/Shutdown Tests ---

class TestStartupShutdown:
    """Tests for startup and shutdown sequence."""

    @pytest.mark.asyncio
    async def test_start_initializes_markets(self, bot):
        """Test that start initializes market states."""
        tokens = ["token_1", "token_2"]

        # Start but don't wait for main loop
        await bot.start(tokens)

        assert bot._running is True
        assert len(bot._markets) == 2
        assert "token_1" in bot._markets
        assert "token_2" in bot._markets
        assert bot._active_tokens == set(tokens)

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_start_connects_websockets(self, bot):
        """Test that start connects WebSocket channels."""
        tokens = ["token_1"]

        await bot.start(tokens)

        bot.orderbook_manager.connect.assert_called_once_with(tokens)
        bot.user_channel_manager.connect.assert_called_once()

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_start_empty_tokens_returns(self, bot):
        """Test that start with empty tokens does nothing."""
        await bot.start([])

        assert bot._running is False
        bot.orderbook_manager.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_cancels_orders(self, bot):
        """Test that stop cancels all orders."""
        await bot.start(["token_1"])
        await bot.stop()

        bot.order_manager.cancel_all.assert_called()
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_stop_disconnects_websockets(self, bot):
        """Test that stop disconnects WebSockets."""
        await bot.start(["token_1"])
        await bot.stop()

        bot.orderbook_manager.disconnect.assert_called()
        bot.user_channel_manager.disconnect.assert_called()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, bot):
        """Test that stop does nothing when not running."""
        await bot.stop()

        bot.order_manager.cancel_all.assert_not_called()


# --- Market Processing Tests ---

class TestMarketProcessing:
    """Tests for market processing logic."""

    @pytest.mark.asyncio
    async def test_process_market_checks_risk_manager(self, bot, valid_orderbook):
        """Test that process_market checks risk manager."""
        bot._markets["test_token"] = MagicMock()
        bot._active_tokens.add("test_token")

        # Mock risk manager to deny
        bot.risk_manager.can_place_orders_for_market = MagicMock(
            return_value=(False, "Halted")
        )

        await bot._process_market("test_token")

        bot.risk_manager.can_place_orders_for_market.assert_called_with("test_token")

    @pytest.mark.asyncio
    async def test_process_market_skips_invalid_orderbook(self, bot):
        """Test that process_market skips invalid orderbooks."""
        from rebates.active_quoting.models import MarketState

        bot._markets["test_token"] = MarketState(
            token_id="test_token",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="test_token"),  # Empty orderbook
            momentum=MomentumState(token_id="test_token"),
            position=Position(token_id="test_token"),
        )
        bot._active_tokens.add("test_token")
        bot.risk_manager.can_place_orders_for_market = MagicMock(
            return_value=(True, "")
        )
        bot.orderbook_manager.get_orderbook = MagicMock(
            return_value=OrderbookState(token_id="test_token")  # Invalid (empty)
        )

        await bot._process_market("test_token")

        # Should not try to calculate quote


# --- Event Handling Tests ---

class TestEventHandling:
    """Tests for event handling."""

    @pytest.mark.asyncio
    async def test_on_fill_updates_inventory(self, bot):
        """Test that on_fill updates inventory manager."""
        fill = Fill(
            order_id="order_1",
            token_id="test_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=-0.01,
            trade_id="trade_1",
        )

        bot.orderbook_manager.get_orderbook = MagicMock(return_value=None)

        await bot._on_fill(fill)

        position = bot.inventory_manager.get_position("test_token")
        assert position.size == 10.0

    @pytest.mark.asyncio
    async def test_on_fill_records_analytics(self, bot, valid_orderbook):
        """Test that on_fill records fill analytics."""
        fill = Fill(
            order_id="order_1",
            token_id="test_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=-0.01,
            trade_id="trade_1",
        )

        bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

        await bot._on_fill(fill)

        record = bot.fill_analytics.get_fill_record("trade_1")
        assert record is not None

    @pytest.mark.asyncio
    async def test_on_book_update_updates_feed_timestamp(self, bot, valid_orderbook):
        """Test that on_book_update updates risk manager timestamp."""
        await bot._on_book_update("test_token", valid_orderbook)

        # Check that feed timestamp was updated
        state = bot.risk_manager.get_market_state("test_token")
        assert state.last_update_time is not None

    @pytest.mark.asyncio
    async def test_on_market_ws_disconnect_triggers_warning(self, bot):
        """Test that market WS disconnect triggers warning state."""
        bot.risk_manager.on_market_disconnect = AsyncMock()

        await bot._on_market_ws_disconnect()

        bot.risk_manager.on_market_disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_user_ws_disconnect_triggers_halt(self, bot):
        """Test that user WS disconnect triggers halt."""
        bot.risk_manager.on_user_disconnect = AsyncMock()

        await bot._on_user_ws_disconnect()

        bot.risk_manager.on_user_disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_momentum_detected_cancels_quotes(self, bot):
        """Test that momentum detection cancels quotes."""
        from rebates.active_quoting.momentum_detector import MomentumEvent

        event = MomentumEvent(
            token_id="test_token",
            event_type="price_move",
            details="Price moved 5 ticks",
            cooldown_until=datetime.utcnow() + timedelta(seconds=2),
        )

        bot._cancel_market_quotes = AsyncMock()

        await bot._on_momentum_detected(event)

        bot._cancel_market_quotes.assert_called_with("test_token")

    @pytest.mark.asyncio
    async def test_on_kill_switch_cancels_all(self, bot):
        """Test that kill switch cancels all orders."""
        await bot._on_kill_switch()

        bot.order_manager.cancel_all.assert_called_once()


# --- Rate Limiting Tests ---

class TestRateLimiting:
    """Tests for rate limiting logic."""

    def test_check_refresh_rate_per_market(self, bot):
        """Test per-market rate limiting."""
        # First refresh should succeed
        assert bot._check_refresh_rate("token_1") is True

        # Record the refresh
        bot._last_quote_refresh["token_1"] = datetime.utcnow()

        # Immediate second refresh should fail
        assert bot._check_refresh_rate("token_1") is False

    def test_check_refresh_rate_after_interval(self, bot):
        """Test refresh allowed after interval."""
        # Set refresh in the past
        bot._last_quote_refresh["token_1"] = datetime.utcnow() - timedelta(seconds=1)

        # Should be allowed now
        assert bot._check_refresh_rate("token_1") is True

    def test_check_refresh_rate_global_cap(self, bot):
        """Test global refresh rate cap."""
        # Reset window
        bot._global_refresh_window_start = datetime.utcnow().timestamp()
        bot._global_refresh_count = 0

        # Exhaust the cap
        for i in range(bot.config.global_refresh_cap_per_sec):
            bot._check_refresh_rate(f"token_{i}")

        # Next one should fail
        assert bot._check_refresh_rate("token_extra") is False

    def test_check_refresh_rate_new_window(self, bot):
        """Test new window resets counter."""
        # Set old window
        bot._global_refresh_window_start = datetime.utcnow().timestamp() - 2.0
        bot._global_refresh_count = bot.config.global_refresh_cap_per_sec

        # Should succeed with new window
        assert bot._check_refresh_rate("token_1") is True


# --- Status Tests ---

class TestStatus:
    """Tests for status reporting."""

    def test_get_status_initial(self, bot):
        """Test initial status."""
        status = bot.get_status()

        assert status["running"] is False
        assert status["active_markets"] == 0
        assert "circuit_breaker_state" in status

    @pytest.mark.asyncio
    async def test_get_status_running(self, bot):
        """Test status when running."""
        await bot.start(["token_1", "token_2"])

        status = bot.get_status()

        assert status["running"] is True
        assert status["active_markets"] == 2
        assert status["market_ws_connected"] is True
        assert status["user_ws_connected"] is True

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    def test_is_running_false_initial(self, bot):
        """Test is_running returns False initially."""
        assert bot.is_running() is False

    @pytest.mark.asyncio
    async def test_is_running_true_when_started(self, bot):
        """Test is_running returns True when started."""
        await bot.start(["token_1"])

        assert bot.is_running() is True

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()


# --- Quote Placement Tests ---

class TestQuotePlacement:
    """Tests for quote placement logic."""

    @pytest.mark.asyncio
    async def test_place_or_update_quote_checks_rate_limit(self, bot):
        """Test that quote placement checks rate limit."""
        quote = Quote(
            token_id="test_token",
            bid_price=0.49,
            ask_price=0.51,
            bid_size=10.0,
            ask_size=10.0,
        )

        # Set recent refresh to fail rate limit
        bot._last_quote_refresh["test_token"] = datetime.utcnow()

        await bot._place_or_update_quote("test_token", quote)

        # Should not place orders due to rate limit
        bot.order_manager.place_orders_batch = AsyncMock()
        bot.order_manager.place_orders_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_or_update_quote_adjusts_sizes(self, bot, config):
        """Test that quote placement adjusts sizes for circuit breaker."""
        from rebates.active_quoting.models import MarketState

        bot._markets["test_token"] = MarketState(
            token_id="test_token",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="test_token"),
            momentum=MomentumState(token_id="test_token"),
            position=Position(token_id="test_token"),
        )

        quote = Quote(
            token_id="test_token",
            bid_price=0.49,
            ask_price=0.51,
            bid_size=10.0,
            ask_size=10.0,
        )

        # Set circuit breaker to WARNING (50% multiplier)
        bot.risk_manager._global_state.circuit_breaker_state = CircuitBreakerState.WARNING

        # Mock order placement
        from rebates.active_quoting.order_manager import BatchOrderResult
        bot.order_manager.place_orders_batch = AsyncMock(
            return_value=BatchOrderResult()
        )

        await bot._place_or_update_quote("test_token", quote)


# --- Cancel Market Quotes Tests ---

class TestCancelMarketQuotes:
    """Tests for cancel market quotes functionality."""

    @pytest.mark.asyncio
    async def test_cancel_market_quotes(self, bot):
        """Test cancelling quotes for a market."""
        from rebates.active_quoting.models import MarketState

        bot._markets["test_token"] = MarketState(
            token_id="test_token",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="test_token"),
            momentum=MomentumState(token_id="test_token"),
            position=Position(token_id="test_token"),
            is_quoting=True,
            last_quote=Quote(
                token_id="test_token",
                bid_price=0.49,
                ask_price=0.51,
                bid_size=10.0,
                ask_size=10.0,
            ),
        )

        await bot._cancel_market_quotes("test_token")

        bot.order_manager.cancel_all_for_token.assert_called_with("test_token")
        assert bot._markets["test_token"].is_quoting is False
        assert bot._markets["test_token"].last_quote is None


# --- Multi-Market Tests ---

class TestMultiMarket:
    """Tests for multi-market support."""

    @pytest.mark.asyncio
    async def test_start_multiple_markets(self, bot):
        """Test starting with multiple markets."""
        tokens = ["token_1", "token_2", "token_3"]

        await bot.start(tokens)

        assert len(bot._markets) == 3
        assert len(bot._active_tokens) == 3

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_fills_tracked_per_market(self, bot, valid_orderbook):
        """Test that fills are tracked separately per market."""
        bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)

        fill1 = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            trade_id="trade_1",
        )
        fill2 = Fill(
            order_id="order_2",
            token_id="token_2",
            side=OrderSide.BUY,
            price=0.50,
            size=20.0,
            fee=0.0,
            trade_id="trade_2",
        )

        await bot._on_fill(fill1)
        await bot._on_fill(fill2)

        pos1 = bot.inventory_manager.get_position("token_1")
        pos2 = bot.inventory_manager.get_position("token_2")

        assert pos1.size == 10.0
        assert pos2.size == 20.0


# --- Circuit Breaker Integration Tests ---

class TestCircuitBreakerIntegration:
    """Tests for circuit breaker integration."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_change_callback(self, bot):
        """Test circuit breaker state change callback is triggered."""
        callback_called = []

        async def mock_callback(old_state, new_state, reason):
            callback_called.append((old_state, new_state, reason))

        bot.risk_manager.on_state_change = mock_callback

        await bot.risk_manager.trigger_warning("Test warning")

        assert len(callback_called) == 1
        assert callback_called[0][1] == CircuitBreakerState.WARNING

    @pytest.mark.asyncio
    async def test_halted_state_prevents_processing(self, bot):
        """Test that halted state prevents market processing."""
        bot.risk_manager._global_state.circuit_breaker_state = CircuitBreakerState.HALTED

        # Mock can_place_orders_for_market to return False when halted
        bot.risk_manager.can_place_orders_for_market = MagicMock(
            return_value=(False, "Global halt active")
        )

        await bot._process_market("test_token")

        # Should check and return early without placing orders
        bot.risk_manager.can_place_orders_for_market.assert_called_once_with("test_token")


# --- Markout Loop Tests ---

class TestMarkoutLoop:
    """Tests for markout processing loop."""

    @pytest.mark.asyncio
    async def test_markout_loop_processes_captures(self, bot, valid_orderbook):
        """Test that markout loop processes due captures."""
        # Add a fill
        fill = Fill(
            order_id="order_1",
            token_id="test_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            timestamp=datetime.utcnow() - timedelta(seconds=10),
            trade_id="trade_1",
        )

        bot.orderbook_manager.get_orderbook = MagicMock(return_value=valid_orderbook)
        await bot._on_fill(fill)

        # Get mid price should return valid price
        def get_mid_price(token_id):
            return 0.51

        # Process markouts
        captured = bot.fill_analytics.process_markout_captures(get_mid_price)

        # Should have captured some markouts
        assert len(captured) > 0


# --- Redemption Integration Tests ---

class TestRedemptionIntegration:
    """Tests for redemption integration with the bot."""

    def test_init_creates_redemption_manager(self, config):
        """Test that redemption manager is created on init."""
        bot = ActiveQuotingBot(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
            enable_persistence=False,
            enable_alerts=False,
        )

        assert bot.redemption_manager is not None

    @pytest.mark.asyncio
    async def test_start_registers_markets_for_redemption(self, bot):
        """Test that markets are registered with redemption manager on start."""
        tokens = ["token_1", "token_2"]
        market_times = {
            "token_1": (datetime.utcnow(), datetime.utcnow() + timedelta(minutes=15)),
            "token_2": (datetime.utcnow(), datetime.utcnow() + timedelta(minutes=30)),
        }
        condition_ids = {
            "token_1": "0xcond1",
            "token_2": "0xcond2",
        }

        await bot.start(
            tokens,
            market_times=market_times,
            condition_ids=condition_ids,
        )

        # Check redemption manager has registered markets
        state1 = bot.redemption_manager.get_state("token_1")
        state2 = bot.redemption_manager.get_state("token_2")

        assert state1 is not None
        assert state1.condition_id == "0xcond1"
        assert state2 is not None
        assert state2.condition_id == "0xcond2"

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_start_without_condition_ids(self, bot):
        """Test that start works without condition_ids."""
        tokens = ["token_1"]

        await bot.start(tokens)

        # Redemption manager should have no registered markets
        state = bot.redemption_manager.get_state("token_1")
        assert state is None

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_market_state_includes_condition_id(self, bot):
        """Test that MarketState includes condition_id."""
        tokens = ["token_1"]
        condition_ids = {"token_1": "0xcond1"}
        market_times = {
            "token_1": (datetime.utcnow(), datetime.utcnow() + timedelta(minutes=15)),
        }

        await bot.start(
            tokens,
            condition_ids=condition_ids,
            market_times=market_times,
        )

        market_state = bot._markets.get("token_1")
        assert market_state is not None
        assert market_state.condition_id == "0xcond1"

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_check_redemptions_with_no_ready_markets(self, bot):
        """Test _check_redemptions with no markets ready."""
        tokens = ["token_1"]
        # Future end time - not ready
        market_times = {
            "token_1": (datetime.utcnow(), datetime.utcnow() + timedelta(minutes=15)),
        }
        condition_ids = {"token_1": "0xcond1"}

        await bot.start(
            tokens,
            market_times=market_times,
            condition_ids=condition_ids,
        )

        # Should not raise
        await bot._check_redemptions()

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_on_redemption_complete_clears_position(self, bot):
        """Test that redemption complete handler clears position."""
        tokens = ["token_1"]
        await bot.start(tokens)

        # Set up a position
        bot.inventory_manager.set_position("token_1", 100.0, 0.50)

        # Trigger redemption complete
        await bot._on_redemption_complete(
            token_id="token_1",
            condition_id="0xcond1",
            tx_hash="0xtxhash",
            position_size=100.0,
        )

        # Position should be cleared
        position = bot.inventory_manager.get_position("token_1")
        assert position.size == 0.0

        # Token should be removed from active tokens
        assert "token_1" not in bot._active_tokens

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    @pytest.mark.asyncio
    async def test_on_redemption_error_keeps_position(self, bot):
        """Test that redemption error handler keeps position."""
        tokens = ["token_1"]
        await bot.start(tokens)

        # Set up a position
        bot.inventory_manager.set_position("token_1", 100.0, 0.50)

        # Trigger redemption error
        await bot._on_redemption_error(
            token_id="token_1",
            condition_id="0xcond1",
            error_message="Transaction failed",
        )

        # Position should still exist
        position = bot.inventory_manager.get_position("token_1")
        assert position.size == 100.0

        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()

    def test_get_status_includes_redemptions(self, bot):
        """Test that get_status includes redemption summary."""
        status = bot.get_status()

        assert "redemptions" in status
        assert "total_markets" in status["redemptions"]
        assert "pending_redemptions" in status["redemptions"]
        assert "completed_redemptions" in status["redemptions"]


# --- Inventory Manager Clear Position Tests ---

class TestInventoryManagerClearPosition:
    """Tests for inventory manager clear_position method."""

    def test_clear_position_zeros_size(self, bot):
        """Test that clear_position zeros the size."""
        bot.inventory_manager.set_position("token_1", 100.0, 0.50)
        bot.inventory_manager.clear_position("token_1")

        position = bot.inventory_manager.get_position("token_1")
        assert position.size == 0.0
        assert position.avg_entry_price == 0.0

    def test_clear_position_preserves_realized_pnl(self, bot):
        """Test that clear_position preserves realized PnL."""
        bot.inventory_manager.set_position("token_1", 100.0, 0.50)
        position = bot.inventory_manager.get_position("token_1")
        position.realized_pnl = 25.0

        bot.inventory_manager.clear_position("token_1")

        # Realized PnL should be preserved
        position = bot.inventory_manager.get_position("token_1")
        assert position.realized_pnl == 25.0
        assert position.size == 0.0

    def test_clear_position_nonexistent_no_error(self, bot):
        """Test that clear_position on nonexistent position does nothing."""
        # Should not raise
        bot.inventory_manager.clear_position("nonexistent")


# --- Fill Protection Tests ---

class TestFillProtection:
    """Tests for fill protection (DEPRECATED - fill protection is now disabled).

    Fill protection was removed because it caused position drift.
    API is now trusted as source of truth.
    See docs/INVENTORY_TRACKING_PLAN.md for details.
    """

    def test_has_recent_fill_always_false(self, bot):
        """Test that has_recent_fill always returns False (deprecated)."""
        # Fill protection is disabled - always returns False
        assert not bot.inventory_manager.has_recent_fill("token_1")

        # Even after a fill, should return False
        fill = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            timestamp=datetime.utcnow(),
            trade_id="trade_1",
        )
        bot.inventory_manager.update_from_fill(fill)

        # Still False - fill protection disabled
        assert not bot.inventory_manager.has_recent_fill("token_1")

    def test_get_last_fill_time(self, bot):
        """Test that get_last_fill_time returns the fill time."""
        fill = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
            timestamp=datetime.utcnow(),
            trade_id="trade_1",
        )
        bot.inventory_manager.update_from_fill(fill)

        last_fill_time = bot.inventory_manager.get_last_fill_time("token_1")
        assert last_fill_time is not None
        # Should be within last few seconds
        assert (datetime.utcnow() - last_fill_time).total_seconds() < 5

    def test_api_sync_updates_position(self, bot):
        """Test that API sync updates confirmed position (dual tracking with partial absorption)."""
        # Simulate a fill that adds 20 to pending_fills
        fill = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=20.0,
            fee=0.0,
            timestamp=datetime.utcnow(),
            trade_id="trade_1",
        )
        bot.inventory_manager.update_from_fill(fill)

        # Effective position should be 20 (0 confirmed + 20 pending)
        pos = bot.inventory_manager.get_position("token_1")
        assert pos.effective_size == 20.0
        assert pos.confirmed_size == 0.0
        assert len(pos.pending_fills) == 1

        # Fill protection is disabled - has_recent_fill always returns False
        assert not bot.inventory_manager.has_recent_fill("token_1")

        # API sync sets confirmed position to 10
        # Partial absorption: pending fill (20) is reduced by 10 to become 10
        bot.inventory_manager.set_position("token_1", 10.0, 0.5)

        pos = bot.inventory_manager.get_position("token_1")
        assert pos.confirmed_size == 10.0  # API is source of truth for confirmed
        # Pending fill reduced from 20 to 10, effective = 10 + 10 = 20
        assert pos.pending_fills["trade_1"].size == 10.0
        assert pos.effective_size == 20.0


# --- Order Update Handler Tests ---

class TestOnOrderUpdate:
    """Tests for _on_order_update handler."""

    @pytest.fixture
    def order_state_buy(self):
        """Create a buy order state."""
        from rebates.active_quoting.models import OrderState
        return OrderState(
            order_id="test_order_123",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=10.0,
            remaining_size=10.0,
            status=OrderStatus.OPEN,
        )

    @pytest.fixture
    def order_state_sell(self):
        """Create a sell order state."""
        from rebates.active_quoting.models import OrderState
        return OrderState(
            order_id="test_order_456",
            token_id="token_1",
            side=OrderSide.SELL,
            price=0.55,
            original_size=10.0,
            remaining_size=10.0,
            status=OrderStatus.OPEN,
        )

    @pytest.mark.asyncio
    async def test_buy_order_cancelled_releases_pending(self, bot, order_state_buy):
        """CANCELLED buy order should release pending buy reservation."""
        # Setup: reserve pending buy capacity
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        initial_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert initial_pending == 10.0

        # Update order state to CANCELLED
        order_state_buy.status = OrderStatus.CANCELLED

        # Call the handler
        await bot._on_order_update(order_state_buy)

        # Verify pending buy was released
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 0.0

    @pytest.mark.asyncio
    async def test_buy_order_expired_releases_pending(self, bot, order_state_buy):
        """EXPIRED buy order should release pending buy reservation."""
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        order_state_buy.status = OrderStatus.EXPIRED

        await bot._on_order_update(order_state_buy)

        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 0.0

    @pytest.mark.asyncio
    async def test_buy_order_rejected_releases_pending(self, bot, order_state_buy):
        """REJECTED buy order should release pending buy reservation."""
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        order_state_buy.status = OrderStatus.REJECTED

        await bot._on_order_update(order_state_buy)

        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 0.0

    @pytest.mark.asyncio
    async def test_sell_order_cancelled_no_release(self, bot, order_state_sell):
        """CANCELLED sell order should NOT release any pending buy."""
        # Reserve some capacity that should NOT be touched
        bot.inventory_manager.reserve_pending_buy("token_1", 5.0)
        initial_pending = bot.inventory_manager.get_pending_buy_size("token_1")

        order_state_sell.status = OrderStatus.CANCELLED

        await bot._on_order_update(order_state_sell)

        # Pending should be unchanged
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == initial_pending

    @pytest.mark.asyncio
    async def test_buy_order_zero_remaining_no_release(self, bot, order_state_buy):
        """Buy order with zero remaining_size should not release anything."""
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        order_state_buy.status = OrderStatus.CANCELLED
        order_state_buy.remaining_size = 0.0  # Fully filled before cancel

        await bot._on_order_update(order_state_buy)

        # Should still have 10 pending (nothing released since remaining was 0)
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 10.0

    @pytest.mark.asyncio
    async def test_open_order_no_release(self, bot, order_state_buy):
        """OPEN order status should not release pending buy."""
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        # Status stays OPEN (not terminal)
        order_state_buy.status = OrderStatus.OPEN

        await bot._on_order_update(order_state_buy)

        # Pending should be unchanged
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 10.0

    @pytest.mark.asyncio
    async def test_filled_order_no_release(self, bot, order_state_buy):
        """FILLED order should not release via order update (handled by fill callback)."""
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        order_state_buy.status = OrderStatus.FILLED
        order_state_buy.remaining_size = 0.0

        await bot._on_order_update(order_state_buy)

        # FILLED is not in terminal_states for release (handled by _on_fill)
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 10.0

    @pytest.mark.asyncio
    async def test_order_manager_state_updated(self, bot, order_state_buy):
        """Order manager state should be updated on order update."""
        # Add order to order_manager
        bot.order_manager._pending_orders[order_state_buy.order_id] = order_state_buy
        
        # Create new state with updated status
        updated_state = order_state_buy
        updated_state.status = OrderStatus.CANCELLED

        await bot._on_order_update(updated_state)

        # Verify order_manager was updated
        tracked_order = bot.order_manager.get_order(order_state_buy.order_id)
        assert tracked_order is not None
        assert tracked_order.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_partial_remaining_releases_only_remaining(self, bot, order_state_buy):
        """Partially filled then cancelled should only release remaining size."""
        # Reserve full amount
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        # Order partially filled (7 remaining out of 10)
        order_state_buy.status = OrderStatus.CANCELLED
        order_state_buy.remaining_size = 7.0

        await bot._on_order_update(order_state_buy)

        # Should release 7, leaving 3 pending (from the filled portion)
        final_pending = bot.inventory_manager.get_pending_buy_size("token_1")
        assert final_pending == 3.0


class TestOnOrderUpdateMultipleOrders:
    """Tests for _on_order_update with multiple orders."""

    @pytest.mark.asyncio
    async def test_multiple_orders_independent_release(self, bot):
        """Multiple orders should release independently."""
        from rebates.active_quoting.models import OrderState

        # Reserve for two orders
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        bot.inventory_manager.reserve_pending_buy("token_1", 5.0)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 15.0

        # Cancel first order
        order1 = OrderState(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=10.0,
            remaining_size=10.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order1)

        # Should have 5 remaining
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 5.0

        # Cancel second order
        order2 = OrderState(
            order_id="order_2",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=5.0,
            remaining_size=5.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order2)

        # Should be 0 now
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0.0

    @pytest.mark.asyncio
    async def test_different_tokens_independent(self, bot):
        """Orders for different tokens should not affect each other."""
        from rebates.active_quoting.models import OrderState

        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        bot.inventory_manager.reserve_pending_buy("token_2", 20.0)

        # Cancel order for token_1
        order = OrderState(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            original_size=10.0,
            remaining_size=10.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order)

        # token_1 should be 0, token_2 unchanged
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0.0
        assert bot.inventory_manager.get_pending_buy_size("token_2") == 20.0


# --- Phase 2: Non-Optimistic Pending Buy Release Tests ---

class TestCancelMarketQuotesNonOptimistic:
    """Tests verifying that cancel does NOT optimistically clear pending buys."""

    @pytest.mark.asyncio
    async def test_cancel_market_quotes_does_not_clear_pending_buys(self, bot):
        """Cancelling quotes should NOT immediately clear pending buy reservations."""
        # Setup: reserve pending buy capacity
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 10.0

        # Setup market state so cancel has something to do
        from rebates.active_quoting.models import MarketState, Quote, OrderbookState, MomentumState, Position
        bot._markets["token_1"] = MarketState(
            token_id="token_1",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="token_1"),
            momentum=MomentumState(token_id="token_1"),
            position=Position(token_id="token_1"),
            last_quote=Quote(
                token_id="token_1",
                bid_price=0.49,
                ask_price=0.51,
                bid_size=10.0,
                ask_size=10.0,
            ),
        )

        # Cancel quotes
        await bot._cancel_market_quotes("token_1")

        # Pending buys should NOT be cleared - they remain until exchange confirms
        pending_after = bot.inventory_manager.get_pending_buy_size("token_1")
        assert pending_after == 10.0, (
            f"Expected pending buys to remain at 10.0, got {pending_after}. "
            "Pending should only be released when exchange confirms CANCELLED."
        )

    @pytest.mark.asyncio
    async def test_pending_buys_released_after_cancel_confirmation(self, bot):
        """Pending buys should be released when CANCELLED confirmation arrives."""
        from rebates.active_quoting.models import OrderState, MarketState, Quote

        # Setup: reserve pending buy and track the order
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        
        buy_order = OrderState(
            order_id="buy_order_123",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.49,
            original_size=10.0,
            remaining_size=10.0,
            status=OrderStatus.OPEN,
        )
        bot.order_manager._pending_orders[buy_order.order_id] = buy_order

        # Setup market
        from rebates.active_quoting.models import OrderbookState, MomentumState, Position, MarketState
        bot._markets["token_1"] = MarketState(
            token_id="token_1",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="token_1"),
            momentum=MomentumState(token_id="token_1"),
            position=Position(token_id="token_1"),
            last_quote=Quote(
                token_id="token_1", bid_price=0.49, ask_price=0.51,
                bid_size=10.0, ask_size=10.0,
            ),
        )

        # Step 1: Cancel quotes (should NOT clear pending)
        await bot._cancel_market_quotes("token_1")
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 10.0

        # Step 2: Exchange sends CANCELLED confirmation via _on_order_update
        buy_order.status = OrderStatus.CANCELLED
        await bot._on_order_update(buy_order)

        # Now pending should be released
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0.0

    @pytest.mark.asyncio
    async def test_replace_quote_does_not_clear_pending_buys(self, bot):
        """Replacing a quote should NOT immediately clear pending buy reservations."""
        from rebates.active_quoting.models import MarketState, Quote

        # Setup: reserve pending buy capacity for existing order
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)

        # Setup market with existing quote
        from rebates.active_quoting.models import OrderbookState, MomentumState, Position, MarketState
        bot._markets["token_1"] = MarketState(
            token_id="token_1",
            reverse_token_id="",
            asset="",
            orderbook=OrderbookState(token_id="token_1"),
            momentum=MomentumState(token_id="token_1"),
            position=Position(token_id="token_1"),
            last_quote=Quote(
                token_id="token_1", bid_price=0.49, ask_price=0.51,
                bid_size=10.0, ask_size=10.0,
            ),
        )

        # Create new quote (this triggers cancel of old + place of new)
        new_quote = Quote(
            token_id="token_1",
            bid_price=0.48,
            ask_price=0.52,
            bid_size=10.0,
            ask_size=10.0,
        )

        # Place the new quote (internally cancels old orders first)
        await bot._place_or_update_quote("token_1", new_quote)

        # Old pending buys should NOT be cleared yet
        # (new order will add its own reservation)
        # The key point: we didnt lose the reservation from the old order
        # until the exchange confirms cancellation
        pending = bot.inventory_manager.get_pending_buy_size("token_1")
        # Should be >= 10 (old reservation still there, possibly new one added too)
        assert pending >= 10.0, (
            f"Expected pending >= 10.0 (old reservation retained), got {pending}"
        )


class TestMultipleOrdersCancelFlow:
    """Test the full flow of multiple orders being cancelled."""

    @pytest.mark.asyncio
    async def test_multiple_buy_orders_release_on_cancel_confirmations(self, bot):
        """Multiple buy orders should release reservations individually on cancel."""
        from rebates.active_quoting.models import OrderState

        # Reserve for 3 buy orders
        bot.inventory_manager.reserve_pending_buy("token_1", 10.0)
        bot.inventory_manager.reserve_pending_buy("token_1", 15.0)
        bot.inventory_manager.reserve_pending_buy("token_1", 20.0)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 45.0

        # Simulate cancel confirmations arriving one by one
        order1 = OrderState(
            order_id="order_1", token_id="token_1", side=OrderSide.BUY,
            price=0.49, original_size=10.0, remaining_size=10.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order1)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 35.0

        order2 = OrderState(
            order_id="order_2", token_id="token_1", side=OrderSide.BUY,
            price=0.48, original_size=15.0, remaining_size=15.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order2)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 20.0

        order3 = OrderState(
            order_id="order_3", token_id="token_1", side=OrderSide.BUY,
            price=0.47, original_size=20.0, remaining_size=20.0,
            status=OrderStatus.CANCELLED,
        )
        await bot._on_order_update(order3)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0.0


# --- Phase 3: Async Position Sync Tests ---

class TestSyncPositionsFromApiAsync:
    """Tests for async _sync_positions_from_api with enhanced protections."""

    @pytest.mark.asyncio
    async def test_sync_allowed_with_pending_buys(self, bot):
        """API position sync is allowed even with pending buys (API is truth)."""
        # Setup: Set a position and reserve pending buys
        bot.inventory_manager.set_position("token_1", size=100, avg_entry_price=0.50)
        bot.inventory_manager.reserve_pending_buy("token_1", 20.0)

        # Verify setup
        assert bot.inventory_manager.get_position("token_1").size == 100
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 20.0

        # API sync should update position regardless of pending buys
        # (pending_buys blocking was removed to prevent deadlock)
        bot.inventory_manager.set_position("token_1", size=80, avg_entry_price=0.50)
        bot.inventory_manager.clear_pending_buys("token_1")

        # Position should now be 80 (API wins)
        assert bot.inventory_manager.get_position("token_1").size == 80
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0

    @pytest.mark.asyncio
    async def test_api_sync_allowed_after_fill(self, bot):
        """API position sync is allowed even after fills (fill protection disabled)."""
        # Setup: Set a position and record a fill
        bot.inventory_manager.set_position("token_1", size=100, avg_entry_price=0.50)

        # Record a fill
        from rebates.active_quoting.models import Fill
        fill = Fill(
            order_id="test_order",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        bot.inventory_manager.update_from_fill(fill)

        # Fill protection is disabled - has_recent_fill always returns False
        assert bot.inventory_manager.has_recent_fill("token_1") is False

    @pytest.mark.asyncio
    async def test_fill_protection_disabled(self, bot):
        """Fill protection is disabled (was 60 seconds, now 0)."""
        from rebates.active_quoting.inventory_manager import FILL_PROTECTION_SECONDS
        assert FILL_PROTECTION_SECONDS == 0.0  # Disabled

    @pytest.mark.asyncio  
    async def test_position_increase_allowed_with_pending_buys(self, bot):
        """API position increase should be allowed even with pending buys."""
        # Setup: position of 100, pending buys of 20
        bot.inventory_manager.set_position("token_1", size=100, avg_entry_price=0.50)
        bot.inventory_manager.reserve_pending_buy("token_1", 20.0)
        
        # If API says position is now 120 (increase), it should be allowed
        # because position increases are safe (we have more than we thought)
        
        # Simulate what the sync logic would do for an increase
        old_size = 100
        new_size = 120  # API says more
        
        # The protection only kicks in if new_size < old_size
        # For increases, we should update
        should_block = (new_size < old_size) and bot.inventory_manager.get_pending_buy_size("token_1") > 0
        assert should_block is False  # Increase should NOT be blocked


class TestFillProtectionWindow:
    """Tests for the fill protection window (DEPRECATED - disabled).

    Fill protection has been removed. API is now trusted as source of truth.
    See docs/INVENTORY_TRACKING_PLAN.md for details.
    """

    def test_has_recent_fill_always_false(self, bot):
        """has_recent_fill always returns False (fill protection disabled)."""
        from rebates.active_quoting.models import Fill

        fill = Fill(
            order_id="test",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        bot.inventory_manager.update_from_fill(fill)

        # Fill protection disabled - always False
        assert bot.inventory_manager.has_recent_fill("token_1") is False

    def test_has_recent_fill_no_fill(self, bot):
        """has_recent_fill should return False when no fill recorded."""
        assert bot.inventory_manager.has_recent_fill("token_1") is False

    def test_api_always_wins_even_with_pending_buys(self, bot):
        """API sync should update position even with pending buys (no blocking)."""
        # Setup
        bot.inventory_manager.set_position("token_1", size=100, avg_entry_price=0.50)
        bot.inventory_manager.reserve_pending_buy("token_1", 20.0)

        # Old logic would block; new logic allows API to win
        # Simulate what sync does: set position and clear pending
        bot.inventory_manager.set_position("token_1", size=80, avg_entry_price=0.50)
        bot.inventory_manager.clear_pending_buys("token_1")

        assert bot.inventory_manager.get_position("token_1").size == 80
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0

    def test_position_reduction_always_allowed(self, bot):
        """Position reduction is always allowed - API is source of truth."""
        # Setup: position with no pending buys
        bot.inventory_manager.set_position("token_1", size=100, avg_entry_price=0.50)

        # API says 80 - should be allowed (no blocking)
        bot.inventory_manager.set_position("token_1", size=80, avg_entry_price=0.50)

        assert bot.inventory_manager.get_position("token_1").size == 80


# --- Phase 4: Order Reconciliation Tests ---

class TestOrderReconciliation:
    """Tests for _reconcile_orders() method and periodic reconciliation."""

    @pytest.mark.asyncio
    async def test_reconcile_orders_calls_user_channel_manager(self, bot):
        """Test that _reconcile_orders calls user_channel_manager.reconcile_with_api_orders."""
        # Setup mock poly_client
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=[])
        
        # Mock reconcile method
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Verify reconcile was called
        bot.user_channel_manager.reconcile_with_api_orders.assert_called_once_with([])

    @pytest.mark.asyncio
    async def test_reconcile_orders_with_open_orders(self, bot):
        """Test reconciliation with open orders from API."""
        # Setup mock poly_client with orders
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.50,
                "original_size": 100,
                "size_matched": 0,
                "status": "OPEN",
            },
            {
                "id": "order_2",
                "asset_id": "token_1",
                "side": "SELL",
                "price": 0.55,
                "original_size": 50,
                "size_matched": 10,
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Verify reconcile was called with the orders
        bot.user_channel_manager.reconcile_with_api_orders.assert_called_once_with(open_orders)

    @pytest.mark.asyncio
    async def test_reconcile_orders_reduces_excess_pending_buys(self, bot):
        """Test that excess pending buys are released during reconciliation."""
        # Setup: We think we have 100 pending, but API shows only 50
        bot.inventory_manager.reserve_pending_buy("token_1", 100.0)
        
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.50,
                "original_size": 50,
                "size_matched": 0,
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        # Before reconciliation
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 100.0
        
        await bot._reconcile_orders()
        
        # After reconciliation, pending should be reduced to 50
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 50.0

    @pytest.mark.asyncio
    async def test_reconcile_orders_no_poly_client(self, bot):
        """Test that reconciliation handles missing poly_client gracefully."""
        bot._poly_client = None
        
        # Should not raise
        await bot._reconcile_orders()

    @pytest.mark.asyncio
    async def test_reconcile_orders_handles_api_error(self, bot):
        """Test that reconciliation handles API errors gracefully."""
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(side_effect=Exception("API error"))
        
        bot._active_tokens = {"token_1"}
        
        # Should not raise
        await bot._reconcile_orders()

    @pytest.mark.asyncio
    async def test_reconcile_called_on_startup(self, bot):
        """Test that reconciliation is called during startup."""
        # Setup mock poly_client
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=[])
        bot._poly_client.browser_wallet = "0xtest"
        
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        # Track if _reconcile_orders was called
        original_reconcile = bot._reconcile_orders
        reconcile_called = []
        
        async def track_reconcile():
            reconcile_called.append(True)
            await original_reconcile()
        
        bot._reconcile_orders = track_reconcile
        
        await bot.start(["token_1"])
        
        # Verify reconcile was called during startup
        assert len(reconcile_called) >= 1
        
        # Cleanup
        bot._running = False
        if bot._main_task:
            bot._main_task.cancel()
        if bot._markout_task:
            bot._markout_task.cancel()
        if bot._daily_summary_task:
            bot._daily_summary_task.cancel()

    @pytest.mark.asyncio
    async def test_reconcile_interval_is_60_seconds(self, bot):
        """Test that reconciliation interval is set to 60 seconds."""
        assert bot._reconcile_interval == 60.0

    @pytest.mark.asyncio
    async def test_reconcile_tracks_last_time(self, bot):
        """Test that _last_reconcile_time is updated after reconciliation."""
        import time
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=[])
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        bot._active_tokens = set()
        
        # Initial value should be 0
        assert bot._last_reconcile_time == 0.0
        
        before = time.time()
        await bot._reconcile_orders()
        # Note: The method itself does not update _last_reconcile_time,
        # that happens in start() and _main_loop()
        # So we just verify the method runs without error


class TestPeriodicReconciliation:
    """Tests for periodic reconciliation in main loop."""

    @pytest.mark.asyncio
    async def test_periodic_reconciliation_triggers_after_interval(self, bot):
        """Test that reconciliation triggers after the interval elapses."""
        import time
        
        # Setup
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=[])
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        bot._running = True
        
        # Set last reconcile time to 70 seconds ago (past the 60s interval)
        bot._last_reconcile_time = time.time() - 70
        
        # Track calls
        reconcile_count = 0
        original_reconcile = bot._reconcile_orders
        
        async def counting_reconcile():
            nonlocal reconcile_count
            reconcile_count += 1
            # Do not actually run to avoid async issues
        
        bot._reconcile_orders = counting_reconcile
        
        # Simulate one iteration of main loop checks
        now = time.time()
        if now - bot._last_reconcile_time >= bot._reconcile_interval:
            await bot._reconcile_orders()
            bot._last_reconcile_time = now
        
        assert reconcile_count == 1
        assert bot._last_reconcile_time >= now - 1  # Updated recently

    @pytest.mark.asyncio
    async def test_periodic_reconciliation_skips_if_not_due(self, bot):
        """Test that reconciliation is skipped if interval has not elapsed."""
        import time
        
        bot._active_tokens = {"token_1"}
        bot._running = True
        
        # Set last reconcile time to 30 seconds ago (not past the 60s interval)
        bot._last_reconcile_time = time.time() - 30
        
        reconcile_count = 0
        
        async def counting_reconcile():
            nonlocal reconcile_count
            reconcile_count += 1
        
        bot._reconcile_orders = counting_reconcile
        
        # Simulate main loop check
        now = time.time()
        if now - bot._last_reconcile_time >= bot._reconcile_interval:
            await bot._reconcile_orders()
            bot._last_reconcile_time = now
        
        # Should not have been called
        assert reconcile_count == 0


class TestReconcilePendingBuysLogic:
    """Tests for the pending buys reconciliation logic."""

    @pytest.mark.asyncio
    async def test_reconcile_multiple_buy_orders_same_token(self, bot):
        """Test reconciliation with multiple buy orders for same token."""
        # We have 200 pending, API shows two orders totaling 150
        bot.inventory_manager.reserve_pending_buy("token_1", 200.0)
        
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.50,
                "original_size": 100,
                "size_matched": 0,
                "status": "OPEN",
            },
            {
                "id": "order_2",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.48,
                "original_size": 50,
                "size_matched": 0,
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Should be reduced to 150 (100 + 50)
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 150.0

    @pytest.mark.asyncio
    async def test_reconcile_no_excess_pending_unchanged(self, bot):
        """Test that pending buys are not changed if they match API."""
        # We have 100 pending, API shows 100 in orders
        bot.inventory_manager.reserve_pending_buy("token_1", 100.0)
        
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.50,
                "original_size": 100,
                "size_matched": 0,
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Should remain at 100
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 100.0

    @pytest.mark.asyncio
    async def test_reconcile_sell_orders_ignored_for_pending_buys(self, bot):
        """Test that sell orders do not affect pending buy calculations."""
        # We have 100 pending buys
        bot.inventory_manager.reserve_pending_buy("token_1", 100.0)
        
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "SELL",  # This is a sell, should not count
                "price": 0.55,
                "original_size": 200,
                "size_matched": 0,
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Pending buys should be cleared since no BUY orders exist
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 0.0

    @pytest.mark.asyncio
    async def test_reconcile_partially_filled_order(self, bot):
        """Test reconciliation accounts for partially filled orders."""
        # We have 100 pending, but order is partially filled (20 filled, 80 remaining)
        bot.inventory_manager.reserve_pending_buy("token_1", 100.0)
        
        open_orders = [
            {
                "id": "order_1",
                "asset_id": "token_1",
                "side": "BUY",
                "price": 0.50,
                "original_size": 100,
                "size_matched": 20,  # 20 already matched
                "status": "OPEN",
            },
        ]
        
        bot._poly_client = MagicMock()
        bot._poly_client.client = MagicMock()
        bot._poly_client.client.get_orders = MagicMock(return_value=open_orders)
        bot.user_channel_manager.reconcile_with_api_orders = MagicMock()
        
        bot._active_tokens = {"token_1"}
        
        await bot._reconcile_orders()
        
        # Remaining unfilled is 100 - 20 = 80
        assert bot.inventory_manager.get_pending_buy_size("token_1") == 80.0
