"""
Unit tests for MomentumDetector.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from rebates.active_quoting.momentum_detector import MomentumDetector, MomentumEvent
from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.models import MomentumState, OrderbookState, OrderbookLevel


@pytest.fixture
def config():
    """Default configuration for tests."""
    return ActiveQuotingConfig(
        momentum_threshold_ticks=3,
        momentum_window_ms=500,
        cooldown_seconds=2.0,
        sweep_depth_threshold=0.5,
    )


@pytest.fixture
def detector(config):
    """MomentumDetector instance with default config."""
    return MomentumDetector(config)


@pytest.fixture
def basic_orderbook():
    """Basic orderbook for testing."""
    return OrderbookState(
        token_id="token1",
        bids=[
            OrderbookLevel(price=0.49, size=100.0),
            OrderbookLevel(price=0.48, size=100.0),
            OrderbookLevel(price=0.47, size=100.0),
            OrderbookLevel(price=0.46, size=100.0),
            OrderbookLevel(price=0.45, size=100.0),
        ],
        asks=[
            OrderbookLevel(price=0.51, size=100.0),
            OrderbookLevel(price=0.52, size=100.0),
            OrderbookLevel(price=0.53, size=100.0),
            OrderbookLevel(price=0.54, size=100.0),
            OrderbookLevel(price=0.55, size=100.0),
        ],
        tick_size=0.01,
    )


class TestMomentumDetectorBasic:
    """Tests for basic momentum detector functionality."""

    def test_initial_state_not_in_cooldown(self, detector):
        """New tokens should not be in cooldown."""
        assert detector.in_cooldown("token1") is False

    def test_get_state_creates_if_not_exists(self, detector):
        """Should create state if it doesn't exist."""
        state = detector.get_state("token1")
        assert state is not None
        assert state.token_id == "token1"
        assert state.is_active is False

    def test_cooldown_remaining_zero_initially(self, detector):
        """Cooldown remaining should be zero initially."""
        remaining = detector.cooldown_remaining_seconds("token1")
        assert remaining == 0


class TestMomentumDetectorPriceMove:
    """Tests for price move momentum detection."""

    @pytest.mark.asyncio
    async def test_no_momentum_single_trade(self, detector):
        """Single trade should not trigger momentum."""
        event = await detector.on_trade("token1", price=0.50, tick_size=0.01)
        assert event is None
        assert detector.in_cooldown("token1") is False

    @pytest.mark.asyncio
    async def test_no_momentum_small_move(self, detector):
        """Small price moves should not trigger momentum."""
        now = datetime.utcnow()

        # Trade 1: 0.50
        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        # Trade 2: 0.51 (1 tick move, < 3 threshold)
        event = await detector.on_trade(
            "token1",
            price=0.51,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        assert event is None
        assert detector.in_cooldown("token1") is False

    @pytest.mark.asyncio
    async def test_momentum_triggered_on_large_move(self, detector):
        """Large price move should trigger momentum."""
        now = datetime.utcnow()

        # Trade 1: 0.50
        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        # Trade 2: 0.54 (4 tick move, >= 3 threshold)
        event = await detector.on_trade(
            "token1",
            price=0.54,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        assert event is not None
        assert event.event_type == "price_move"
        assert detector.in_cooldown("token1") is True

    @pytest.mark.asyncio
    async def test_momentum_exactly_at_threshold(self, detector):
        """Price move exactly at threshold should trigger."""
        now = datetime.utcnow()

        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        # 3 tick move, exactly at threshold
        event = await detector.on_trade(
            "token1",
            price=0.53,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        assert event is not None
        assert detector.in_cooldown("token1") is True

    @pytest.mark.asyncio
    async def test_no_momentum_outside_window(self, detector):
        """Price move outside time window should not trigger.

        Note: The MomentumState.price_change_ticks() uses datetime.utcnow()
        to determine the window, so we need to mock the time to test this.
        """
        from unittest.mock import patch

        # Create a fixed "current time" for the test
        base_time = datetime(2026, 1, 1, 12, 0, 0)

        # Trade 1: 0.50 at time T
        with patch('rebates.active_quoting.models.datetime') as mock_datetime:
            mock_datetime.utcnow.return_value = base_time
            await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=base_time)

        # Trade 2: 0.54 at time T+1s (outside 500ms window)
        # When checking for momentum, use time T+1s
        with patch('rebates.active_quoting.models.datetime') as mock_datetime:
            mock_datetime.utcnow.return_value = base_time + timedelta(milliseconds=1000)
            event = await detector.on_trade(
                "token1",
                price=0.54,
                tick_size=0.01,
                timestamp=base_time + timedelta(milliseconds=1000)
            )

        # First trade should be outside the window (more than 500ms ago)
        # So only 1 trade in window = no momentum detected
        assert event is None

    @pytest.mark.asyncio
    async def test_callback_called_on_momentum(self, config):
        """Callback should be called when momentum is detected."""
        callback = AsyncMock()
        detector = MomentumDetector(config, on_momentum=callback)

        now = datetime.utcnow()
        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        await detector.on_trade(
            "token1",
            price=0.54,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        callback.assert_called_once()
        event = callback.call_args[0][0]
        assert isinstance(event, MomentumEvent)
        assert event.token_id == "token1"


class TestMomentumDetectorBookSweep:
    """Tests for book sweep detection."""

    @pytest.mark.asyncio
    async def test_no_sweep_on_first_update(self, detector, basic_orderbook):
        """First orderbook update should not trigger sweep."""
        event = await detector.on_orderbook_update(basic_orderbook)
        assert event is None

    @pytest.mark.asyncio
    async def test_no_sweep_small_depth_change(self, detector, basic_orderbook):
        """Small depth changes should not trigger sweep."""
        # First update to establish baseline
        await detector.on_orderbook_update(basic_orderbook)

        # Second update with 30% bid depth drop (< 50% threshold)
        reduced_orderbook = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.49, size=70.0),  # 70% remaining
                OrderbookLevel(price=0.48, size=70.0),
                OrderbookLevel(price=0.47, size=70.0),
                OrderbookLevel(price=0.46, size=70.0),
                OrderbookLevel(price=0.45, size=70.0),
            ],
            asks=[
                OrderbookLevel(price=0.51, size=100.0),
                OrderbookLevel(price=0.52, size=100.0),
                OrderbookLevel(price=0.53, size=100.0),
                OrderbookLevel(price=0.54, size=100.0),
                OrderbookLevel(price=0.55, size=100.0),
            ],
            tick_size=0.01,
        )

        event = await detector.on_orderbook_update(reduced_orderbook)
        assert event is None

    @pytest.mark.asyncio
    async def test_sweep_triggered_on_large_depth_drop(self, detector, basic_orderbook):
        """Large depth drop should trigger sweep detection."""
        # First update to establish baseline (500 total bid depth)
        await detector.on_orderbook_update(basic_orderbook)

        # Second update with 60% bid depth drop (>= 50% threshold)
        swept_orderbook = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.49, size=40.0),  # 40% remaining per level
                OrderbookLevel(price=0.48, size=40.0),
                OrderbookLevel(price=0.47, size=40.0),
                OrderbookLevel(price=0.46, size=40.0),
                OrderbookLevel(price=0.45, size=40.0),
            ],
            asks=[
                OrderbookLevel(price=0.51, size=100.0),
                OrderbookLevel(price=0.52, size=100.0),
                OrderbookLevel(price=0.53, size=100.0),
                OrderbookLevel(price=0.54, size=100.0),
                OrderbookLevel(price=0.55, size=100.0),
            ],
            tick_size=0.01,
        )

        event = await detector.on_orderbook_update(swept_orderbook)
        assert event is not None
        assert event.event_type == "book_sweep"
        assert "Bid depth dropped" in event.details
        assert detector.in_cooldown("token1") is True

    @pytest.mark.asyncio
    async def test_sweep_on_ask_side(self, detector, basic_orderbook):
        """Ask side sweep should also trigger."""
        await detector.on_orderbook_update(basic_orderbook)

        # Sweep on ask side
        swept_orderbook = OrderbookState(
            token_id="token1",
            bids=[
                OrderbookLevel(price=0.49, size=100.0),
                OrderbookLevel(price=0.48, size=100.0),
                OrderbookLevel(price=0.47, size=100.0),
                OrderbookLevel(price=0.46, size=100.0),
                OrderbookLevel(price=0.45, size=100.0),
            ],
            asks=[
                OrderbookLevel(price=0.51, size=40.0),  # 40% remaining
                OrderbookLevel(price=0.52, size=40.0),
                OrderbookLevel(price=0.53, size=40.0),
                OrderbookLevel(price=0.54, size=40.0),
                OrderbookLevel(price=0.55, size=40.0),
            ],
            tick_size=0.01,
        )

        event = await detector.on_orderbook_update(swept_orderbook)
        assert event is not None
        assert event.event_type == "book_sweep"
        assert "Ask depth dropped" in event.details


class TestMomentumDetectorCooldown:
    """Tests for cooldown management."""

    @pytest.mark.asyncio
    async def test_cooldown_duration(self, detector):
        """Cooldown should last for configured duration."""
        now = datetime.utcnow()

        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        await detector.on_trade(
            "token1",
            price=0.54,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        # Should be in cooldown
        assert detector.in_cooldown("token1") is True

        # Remaining should be close to 2 seconds
        remaining = detector.cooldown_remaining_seconds("token1")
        assert 1.5 < remaining <= 2.0

    def test_check_cooldown_expired_not_active(self, detector):
        """Should return False when not in cooldown."""
        result = detector.check_cooldown_expired("token1")
        assert result is False

    def test_check_cooldown_expired_still_active(self, detector):
        """Should return False when cooldown is still active."""
        detector.force_cooldown("token1", seconds=10)

        result = detector.check_cooldown_expired("token1")
        assert result is False
        assert detector.in_cooldown("token1") is True

    def test_check_cooldown_expired_just_expired(self, detector):
        """Should return True when cooldown just expired."""
        # Force a cooldown that's already expired
        state = detector.get_state("token1")
        state.is_active = True
        state.cooldown_until = datetime.utcnow() - timedelta(seconds=1)

        result = detector.check_cooldown_expired("token1")
        assert result is True
        assert detector.in_cooldown("token1") is False
        assert state.is_active is False

    def test_force_cooldown(self, detector):
        """Should force a cooldown period."""
        event = detector.force_cooldown("token1", seconds=5.0, reason="test")

        assert event.event_type == "forced"
        assert event.details == "test"
        assert detector.in_cooldown("token1") is True
        remaining = detector.cooldown_remaining_seconds("token1")
        assert 4.5 < remaining <= 5.0

    def test_force_cooldown_default_duration(self, detector, config):
        """Force cooldown should use config duration by default."""
        event = detector.force_cooldown("token1")

        remaining = detector.cooldown_remaining_seconds("token1")
        assert remaining <= config.cooldown_seconds

    def test_clear_cooldown(self, detector):
        """Should clear cooldown."""
        detector.force_cooldown("token1", seconds=5.0)
        assert detector.in_cooldown("token1") is True

        detector.clear_cooldown("token1")
        assert detector.in_cooldown("token1") is False


class TestMomentumDetectorReset:
    """Tests for reset functionality."""

    @pytest.mark.asyncio
    async def test_reset_single_token(self, detector):
        """Should reset state for a single token."""
        await detector.on_trade("token1", price=0.50, tick_size=0.01)
        detector.force_cooldown("token1")

        detector.reset("token1")

        assert "token1" not in detector.states

    def test_reset_all(self, detector):
        """Should reset all states."""
        detector.force_cooldown("token1")
        detector.force_cooldown("token2")

        detector.reset_all()

        assert len(detector.states) == 0


class TestMomentumDetectorSummary:
    """Tests for summary and monitoring."""

    @pytest.mark.asyncio
    async def test_get_summary(self, detector):
        """Should return summary of states."""
        await detector.on_trade("token1", price=0.50, tick_size=0.01)
        detector.force_cooldown("token1", seconds=5.0)

        summary = detector.get_summary()

        assert "token1" in summary
        assert summary["token1"]["in_cooldown"] is True
        assert summary["token1"]["trade_count"] == 1
        assert summary["token1"]["last_trade_price"] == 0.50

    def test_get_active_cooldowns(self, detector):
        """Should return only active cooldowns."""
        detector.force_cooldown("token1", seconds=5.0)
        detector.force_cooldown("token2", seconds=5.0)
        detector.get_state("token3")  # Not in cooldown

        active = detector.get_active_cooldowns()

        assert "token1" in active
        assert "token2" in active
        assert "token3" not in active

    def test_get_active_cooldowns_empty(self, detector):
        """Should return empty dict when no cooldowns."""
        active = detector.get_active_cooldowns()
        assert active == {}


class TestMomentumDetectorEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_very_small_tick_size(self, config):
        """Should handle very small tick sizes."""
        config.momentum_threshold_ticks = 3
        detector = MomentumDetector(config)

        now = datetime.utcnow()
        await detector.on_trade("token1", price=0.500, tick_size=0.001, timestamp=now)
        # 5 tick move at 0.001 tick size
        event = await detector.on_trade(
            "token1",
            price=0.505,
            tick_size=0.001,
            timestamp=now + timedelta(milliseconds=100)
        )

        assert event is not None

    @pytest.mark.asyncio
    async def test_multiple_tokens_independent(self, detector):
        """Different tokens should have independent states."""
        now = datetime.utcnow()

        # Trigger momentum on token1
        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        await detector.on_trade(
            "token1",
            price=0.54,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        # token2 should not be affected
        await detector.on_trade("token2", price=0.50, tick_size=0.01, timestamp=now)

        assert detector.in_cooldown("token1") is True
        assert detector.in_cooldown("token2") is False

    @pytest.mark.asyncio
    async def test_rapid_trades_within_window(self, detector):
        """Multiple rapid trades should be handled correctly."""
        now = datetime.utcnow()

        # 5 trades moving up 1 tick each
        prices = [0.50, 0.51, 0.52, 0.53, 0.54]
        for i, price in enumerate(prices):
            event = await detector.on_trade(
                "token1",
                price=price,
                tick_size=0.01,
                timestamp=now + timedelta(milliseconds=i * 50)
            )
            if i >= 3:  # Should trigger on 4th trade (3 ticks)
                assert event is not None
                break

    @pytest.mark.asyncio
    async def test_price_down_move_also_triggers(self, detector):
        """Downward price moves should also trigger momentum."""
        now = datetime.utcnow()

        await detector.on_trade("token1", price=0.54, tick_size=0.01, timestamp=now)
        # Price drops by 4 ticks
        event = await detector.on_trade(
            "token1",
            price=0.50,
            tick_size=0.01,
            timestamp=now + timedelta(milliseconds=100)
        )

        assert event is not None
        assert detector.in_cooldown("token1") is True

    @pytest.mark.asyncio
    async def test_zero_depth_handled_gracefully(self, detector):
        """Should handle zero depth without error."""
        # First update with zero depth
        empty_orderbook = OrderbookState(
            token_id="token1",
            bids=[],
            asks=[],
            tick_size=0.01,
        )

        event = await detector.on_orderbook_update(empty_orderbook)
        assert event is None

    @pytest.mark.asyncio
    async def test_depth_to_zero_is_sweep(self, detector, basic_orderbook):
        """Complete depth removal should trigger sweep."""
        await detector.on_orderbook_update(basic_orderbook)

        empty_orderbook = OrderbookState(
            token_id="token1",
            bids=[],
            asks=[],
            tick_size=0.01,
        )

        event = await detector.on_orderbook_update(empty_orderbook)
        assert event is not None
        assert event.event_type == "book_sweep"


class TestMomentumDetectorTradeHistory:
    """Tests for trade history management."""

    @pytest.mark.asyncio
    async def test_trade_history_limited(self, detector):
        """Trade history should be limited to prevent memory growth."""
        now = datetime.utcnow()

        # Add 150 trades (limit is 100)
        for i in range(150):
            await detector.on_trade(
                "token1",
                price=0.50 + (i % 3) * 0.01,
                tick_size=0.01,
                timestamp=now + timedelta(seconds=i)
            )

        state = detector.get_state("token1")
        assert len(state.last_trade_prices) <= 100
        assert len(state.last_trade_times) <= 100

    @pytest.mark.asyncio
    async def test_trade_timestamps_tracked(self, detector):
        """Trade timestamps should be tracked correctly."""
        now = datetime.utcnow()

        await detector.on_trade("token1", price=0.50, tick_size=0.01, timestamp=now)
        await detector.on_trade(
            "token1",
            price=0.51,
            tick_size=0.01,
            timestamp=now + timedelta(seconds=1)
        )

        state = detector.get_state("token1")
        assert len(state.last_trade_times) == 2
        assert state.last_trade_times[0] == now
