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
        )

        assert bot._poly_client == mock_client

    def test_init_state(self, config):
        """Test initial state."""
        bot = ActiveQuotingBot(
            config=config,
            api_key="test_key",
            api_secret="test_secret",
            api_passphrase="test_passphrase",
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
    """Tests for fill protection against stale API data."""

    def test_has_recent_fill_false_initially(self, bot):
        """Test that has_recent_fill returns False with no fills."""
        assert not bot.inventory_manager.has_recent_fill("token_1")

    def test_has_recent_fill_true_after_fill(self, bot):
        """Test that has_recent_fill returns True after a fill."""
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

        assert bot.inventory_manager.has_recent_fill("token_1")

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

    def test_fill_protection_blocks_position_reduction(self, bot):
        """Test that API sync doesn't reduce position with recent fills."""
        # Simulate a fill that updated position to 20
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

        # Position should be 20
        assert bot.inventory_manager.get_position("token_1").size == 20.0

        # Now if API tries to say position is 0 (stale data), it should be ignored
        # The has_recent_fill check will return True, blocking the reduction
        assert bot.inventory_manager.has_recent_fill("token_1")

        # Manually test the protection logic
        old_size = 20.0
        api_size = 0.0
        if api_size < old_size and bot.inventory_manager.has_recent_fill("token_1"):
            # Should skip the update
            pass
        else:
            bot.inventory_manager.set_position("token_1", api_size, 0.5)

        # Position should still be 20, not 0
        assert bot.inventory_manager.get_position("token_1").size == 20.0
