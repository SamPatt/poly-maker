"""
Unit tests for RedemptionManager - Market resolution and position redemption.

Tests:
- Market registration
- Resolution timing with configurable delays
- Redemption state transitions
- Success and error callbacks
- Retry logic for "not resolved" errors
- Position tracking
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from rebates.active_quoting.redemption_manager import (
    RedemptionManager,
    RedemptionState,
    MarketRedemptionState,
)


@pytest.fixture
def redemption_manager():
    """Create RedemptionManager with short delays for testing."""
    return RedemptionManager(
        resolution_check_delay_seconds=1.0,  # Short for testing
        resolution_check_interval_seconds=0.5,
        max_resolution_check_attempts=5,
    )


@pytest.fixture
def redemption_manager_with_callbacks():
    """Create RedemptionManager with mock callbacks."""
    on_complete = AsyncMock()
    on_error = AsyncMock()
    manager = RedemptionManager(
        on_redemption_complete=on_complete,
        on_redemption_error=on_error,
        resolution_check_delay_seconds=1.0,
        resolution_check_interval_seconds=0.5,
        max_resolution_check_attempts=5,
    )
    return manager, on_complete, on_error


# --- MarketRedemptionState Tests ---

class TestMarketRedemptionState:
    """Tests for MarketRedemptionState dataclass."""

    def test_initial_state(self):
        """Test initial market redemption state."""
        state = MarketRedemptionState(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
        )
        assert state.token_id == "token1"
        assert state.condition_id == "0xabc123"
        assert state.position_size == 0.0
        assert state.state == RedemptionState.PENDING
        assert state.first_check_time is None
        assert state.check_count == 0
        assert state.tx_hash is None
        assert state.error_message is None

    def test_state_with_position(self):
        """Test state with position size."""
        state = MarketRedemptionState(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=100.0,
        )
        assert state.position_size == 100.0


# --- Registration Tests ---

class TestRegistration:
    """Tests for market registration."""

    def test_register_market(self, redemption_manager):
        """Test registering a market for redemption tracking."""
        end_time = datetime.utcnow() + timedelta(minutes=15)

        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        state = redemption_manager.get_state("token1")
        assert state is not None
        assert state.token_id == "token1"
        assert state.condition_id == "0xabc123"
        assert state.market_end_time == end_time
        assert state.position_size == 50.0
        assert state.state == RedemptionState.PENDING

    def test_register_market_no_condition_id_skipped(self, redemption_manager):
        """Test that registration is skipped without condition_id."""
        redemption_manager.register_market(
            token_id="token1",
            condition_id="",  # Empty
            market_end_time=datetime.utcnow(),
        )

        state = redemption_manager.get_state("token1")
        assert state is None

    def test_register_market_already_redeemed_skipped(self, redemption_manager):
        """Test that already-redeemed markets are not re-registered."""
        # Simulate completed redemption
        redemption_manager._completed_redemptions.add("token1")

        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
        )

        state = redemption_manager.get_state("token1")
        assert state is None

    def test_update_position_size(self, redemption_manager):
        """Test updating position size."""
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )

        redemption_manager.update_position_size("token1", 100.0)

        state = redemption_manager.get_state("token1")
        assert state.position_size == 100.0

    def test_update_position_size_unregistered(self, redemption_manager):
        """Test updating position size for unregistered market does nothing."""
        # Should not raise
        redemption_manager.update_position_size("nonexistent", 100.0)


# --- Resolution Check Timing Tests ---

class TestResolutionCheckTiming:
    """Tests for resolution check timing logic."""

    def test_get_markets_ready_before_end_time(self, redemption_manager):
        """Test no markets ready before end time."""
        end_time = datetime.utcnow() + timedelta(minutes=15)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 0

    def test_get_markets_ready_before_delay(self):
        """Test no markets ready during delay period after end time."""
        # Use a manager with 60 second delay
        manager = RedemptionManager(
            resolution_check_delay_seconds=60.0,  # 60 second delay
            resolution_check_interval_seconds=0.5,
            max_resolution_check_attempts=5,
        )
        # End time was 30 seconds ago, but delay is 60 seconds
        end_time = datetime.utcnow() - timedelta(seconds=30)
        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        ready = manager.get_markets_ready_for_check()
        assert len(ready) == 0

    def test_get_markets_ready_after_delay(self, redemption_manager):
        """Test markets ready after delay period."""
        # End time was 2 seconds ago, delay is 1 second
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 1
        assert ready[0].token_id == "token1"

    def test_get_markets_ready_respects_check_interval(self, redemption_manager):
        """Test that check interval is respected between checks."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        state = redemption_manager.get_state("token1")
        # Simulate recent check
        state.last_check_time = datetime.utcnow()
        state.check_count = 1

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 0  # Not ready yet due to interval

    def test_get_markets_ready_max_attempts_exceeded(self, redemption_manager):
        """Test markets not ready after max check attempts."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        state = redemption_manager.get_state("token1")
        state.check_count = 10  # Exceeds max of 5

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 0
        assert state.state == RedemptionState.FAILED

    def test_get_markets_ready_skips_pending_redemptions(self, redemption_manager):
        """Test that markets with redemption in progress are skipped."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        redemption_manager._pending_redemptions.add("token1")

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 0

    def test_get_markets_ready_skips_completed(self, redemption_manager):
        """Test that completed markets are skipped."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        redemption_manager._completed_redemptions.add("token1")

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 0


# --- Redemption Attempt Tests ---

class TestRedemptionAttempt:
    """Tests for redemption attempt logic."""

    @pytest.mark.asyncio
    async def test_attempt_redemption_no_position(self, redemption_manager):
        """Test that redemption is skipped with no position."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=0.0,
        )

        result = await redemption_manager.attempt_redemption("token1", 0.0)

        assert result is False
        state = redemption_manager.get_state("token1")
        assert state.state == RedemptionState.SKIPPED
        assert redemption_manager.is_redemption_complete("token1")

    @pytest.mark.asyncio
    async def test_attempt_redemption_unregistered_market(self, redemption_manager):
        """Test redemption attempt for unregistered market."""
        result = await redemption_manager.attempt_redemption("nonexistent", 50.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_attempt_redemption_initiates(self, redemption_manager):
        """Test that redemption is initiated with position."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        with patch('redemption.redeem_position_async') as mock_redeem:
            result = await redemption_manager.attempt_redemption("token1", 50.0)

            assert result is True
            mock_redeem.assert_called_once()
            args, kwargs = mock_redeem.call_args
            assert args[0] == "0xabc123"  # condition_id

            state = redemption_manager.get_state("token1")
            assert state.state == RedemptionState.REDEEMING
            assert redemption_manager.is_redemption_pending("token1")

    @pytest.mark.asyncio
    async def test_attempt_redemption_tracks_check_count(self, redemption_manager):
        """Test that check count is tracked."""
        end_time = datetime.utcnow() - timedelta(seconds=2)
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=50.0,
        )

        with patch('redemption.redeem_position_async'):
            await redemption_manager.attempt_redemption("token1", 50.0)

            state = redemption_manager.get_state("token1")
            assert state.check_count == 1
            assert state.first_check_time is not None
            assert state.last_check_time is not None


# --- Callback Tests ---

class TestCallbacks:
    """Tests for success and error callbacks."""

    @pytest.mark.asyncio
    async def test_handle_redemption_success(self):
        """Test successful redemption callback."""
        on_complete = AsyncMock()
        manager = RedemptionManager(on_redemption_complete=on_complete)

        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )
        manager._pending_redemptions.add("token1")

        await manager._handle_redemption_success("token1", "0xabc123", "0xtxhash123")

        state = manager.get_state("token1")
        assert state.state == RedemptionState.COMPLETED
        assert state.tx_hash == "0xtxhash123"
        assert manager.is_redemption_complete("token1")
        assert not manager.is_redemption_pending("token1")

        on_complete.assert_called_once_with("token1", "0xabc123", "0xtxhash123", 50.0)

    @pytest.mark.asyncio
    async def test_handle_redemption_error_final_failure(self):
        """Test final failure redemption callback."""
        on_error = AsyncMock()
        manager = RedemptionManager(on_redemption_error=on_error)

        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )
        manager._pending_redemptions.add("token1")
        state = manager.get_state("token1")
        state.check_count = 10  # Exceeds max

        await manager._handle_redemption_error("token1", "0xabc123", "Transaction failed")

        assert state.state == RedemptionState.FAILED
        assert state.error_message == "Transaction failed"
        assert not manager.is_redemption_pending("token1")

        on_error.assert_called_once_with("token1", "0xabc123", "Transaction failed")

    @pytest.mark.asyncio
    async def test_handle_redemption_error_not_resolved_retry(self):
        """Test that 'not resolved' errors trigger retry."""
        on_error = AsyncMock()
        manager = RedemptionManager(
            on_redemption_error=on_error,
            max_resolution_check_attempts=5,
        )

        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )
        manager._pending_redemptions.add("token1")
        state = manager.get_state("token1")
        state.check_count = 1  # Below max

        await manager._handle_redemption_error("token1", "0xabc123", "condition not resolved")

        # Should go back to CHECKING for retry
        assert state.state == RedemptionState.CHECKING
        assert not manager.is_redemption_pending("token1")
        on_error.assert_not_called()  # Not a final failure

    @pytest.mark.asyncio
    async def test_handle_redemption_error_not_resolved_max_attempts(self):
        """Test that 'not resolved' errors fail after max attempts."""
        on_error = AsyncMock()
        manager = RedemptionManager(
            on_redemption_error=on_error,
            max_resolution_check_attempts=5,
        )

        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )
        manager._pending_redemptions.add("token1")
        state = manager.get_state("token1")
        state.check_count = 5  # At max

        await manager._handle_redemption_error("token1", "0xabc123", "condition not resolved")

        # Should be final failure
        assert state.state == RedemptionState.FAILED
        on_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_redemption_error_payout_not_set_retry(self):
        """Test that 'payout not set' errors trigger retry."""
        manager = RedemptionManager(max_resolution_check_attempts=5)

        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )
        manager._pending_redemptions.add("token1")
        state = manager.get_state("token1")
        state.check_count = 1

        await manager._handle_redemption_error("token1", "0xabc123", "payoutDenominator is 0")

        assert state.state == RedemptionState.CHECKING  # Retry


# --- State Query Tests ---

class TestStateQueries:
    """Tests for state query methods."""

    def test_is_redemption_complete_false(self, redemption_manager):
        """Test is_redemption_complete returns False for non-completed."""
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
        )
        assert not redemption_manager.is_redemption_complete("token1")

    def test_is_redemption_complete_true(self, redemption_manager):
        """Test is_redemption_complete returns True for completed."""
        redemption_manager._completed_redemptions.add("token1")
        assert redemption_manager.is_redemption_complete("token1")

    def test_is_redemption_pending_false(self, redemption_manager):
        """Test is_redemption_pending returns False when not pending."""
        assert not redemption_manager.is_redemption_pending("token1")

    def test_is_redemption_pending_true(self, redemption_manager):
        """Test is_redemption_pending returns True when pending."""
        redemption_manager._pending_redemptions.add("token1")
        assert redemption_manager.is_redemption_pending("token1")


# --- Summary Tests ---

class TestSummary:
    """Tests for summary method."""

    def test_get_summary_empty(self, redemption_manager):
        """Test summary with no markets."""
        summary = redemption_manager.get_summary()

        assert summary["total_markets"] == 0
        assert summary["pending_redemptions"] == 0
        assert summary["completed_redemptions"] == 0
        assert "states" in summary

    def test_get_summary_with_markets(self, redemption_manager):
        """Test summary with markets in various states."""
        # Register a pending market
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
            position_size=50.0,
        )

        # Simulate a completed market
        redemption_manager._completed_redemptions.add("token2")

        # Simulate a pending redemption
        redemption_manager._pending_redemptions.add("token3")

        summary = redemption_manager.get_summary()

        assert summary["total_markets"] == 1  # Only registered markets
        assert summary["pending_redemptions"] == 1
        assert summary["completed_redemptions"] == 1
        assert summary["states"]["PENDING"] == 1


# --- Clear Market Tests ---

class TestClearMarket:
    """Tests for clear_market method."""

    def test_clear_market(self, redemption_manager):
        """Test clearing a market from tracking."""
        redemption_manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=datetime.utcnow(),
        )
        redemption_manager._pending_redemptions.add("token1")

        redemption_manager.clear_market("token1")

        assert redemption_manager.get_state("token1") is None
        assert not redemption_manager.is_redemption_pending("token1")

    def test_clear_market_preserves_completed(self, redemption_manager):
        """Test that clear_market does not remove from completed set."""
        redemption_manager._completed_redemptions.add("token1")

        redemption_manager.clear_market("token1")

        assert redemption_manager.is_redemption_complete("token1")


# --- Multiple Markets Tests ---

class TestMultipleMarkets:
    """Tests for handling multiple markets."""

    def test_multiple_markets_ready(self, redemption_manager):
        """Test getting multiple markets ready for check."""
        end_time = datetime.utcnow() - timedelta(seconds=2)

        for i in range(3):
            redemption_manager.register_market(
                token_id=f"token{i}",
                condition_id=f"0xcond{i}",
                market_end_time=end_time,
                position_size=50.0,
            )

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 3

    def test_mixed_states_multiple_markets(self, redemption_manager):
        """Test multiple markets in different states."""
        end_time_past = datetime.utcnow() - timedelta(seconds=2)
        end_time_future = datetime.utcnow() + timedelta(minutes=15)

        # Ready market
        redemption_manager.register_market(
            token_id="ready",
            condition_id="0xready",
            market_end_time=end_time_past,
            position_size=50.0,
        )

        # Not ready yet (future)
        redemption_manager.register_market(
            token_id="future",
            condition_id="0xfuture",
            market_end_time=end_time_future,
            position_size=50.0,
        )

        # Completed
        redemption_manager.register_market(
            token_id="completed",
            condition_id="0xcompleted",
            market_end_time=end_time_past,
            position_size=50.0,
        )
        redemption_manager._completed_redemptions.add("completed")

        ready = redemption_manager.get_markets_ready_for_check()
        assert len(ready) == 1
        assert ready[0].token_id == "ready"


# --- Integration-like Tests ---

class TestRedemptionFlow:
    """Integration-like tests for complete redemption flow."""

    @pytest.mark.asyncio
    async def test_full_redemption_flow_success(self):
        """Test complete redemption flow with success."""
        on_complete = AsyncMock()
        manager = RedemptionManager(
            on_redemption_complete=on_complete,
            resolution_check_delay_seconds=0.1,
            resolution_check_interval_seconds=0.1,
            max_resolution_check_attempts=5,
        )

        end_time = datetime.utcnow() - timedelta(seconds=1)
        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=100.0,
        )

        # Wait for delay
        await asyncio.sleep(0.15)

        # Check markets ready
        ready = manager.get_markets_ready_for_check()
        assert len(ready) == 1

        # Attempt redemption
        with patch('redemption.redeem_position_async') as mock_redeem:
            # Capture the callbacks passed to redeem_position_async
            def capture_callbacks(condition_id, on_success, on_error):
                # Simulate successful redemption
                on_success(condition_id, "0xtxhash")

            mock_redeem.side_effect = capture_callbacks

            result = await manager.attempt_redemption("token1", 100.0)
            assert result is True

        # Wait for async callback processing
        await asyncio.sleep(0.1)

        # Verify completion
        assert manager.is_redemption_complete("token1")
        state = manager.get_state("token1")
        assert state.state == RedemptionState.COMPLETED
        assert state.tx_hash == "0xtxhash"

        on_complete.assert_called_once_with("token1", "0xabc123", "0xtxhash", 100.0)

    @pytest.mark.asyncio
    async def test_full_redemption_flow_retry_then_success(self):
        """Test redemption flow with retry then success."""
        on_complete = AsyncMock()
        manager = RedemptionManager(
            on_redemption_complete=on_complete,
            resolution_check_delay_seconds=0.1,
            resolution_check_interval_seconds=0.1,
            max_resolution_check_attempts=5,
        )

        end_time = datetime.utcnow() - timedelta(seconds=1)
        manager.register_market(
            token_id="token1",
            condition_id="0xabc123",
            market_end_time=end_time,
            position_size=100.0,
        )

        await asyncio.sleep(0.15)

        # First attempt - not resolved
        with patch('redemption.redeem_position_async') as mock_redeem:
            def fail_first(condition_id, on_success, on_error):
                on_error(condition_id, "condition not resolved")

            mock_redeem.side_effect = fail_first
            await manager.attempt_redemption("token1", 100.0)

        await asyncio.sleep(0.1)

        # Should be back to CHECKING for retry
        state = manager.get_state("token1")
        assert state.state == RedemptionState.CHECKING
        assert state.check_count == 1

        # Wait for interval
        await asyncio.sleep(0.15)

        # Second attempt - success
        ready = manager.get_markets_ready_for_check()
        assert len(ready) == 1

        with patch('redemption.redeem_position_async') as mock_redeem:
            def succeed_second(condition_id, on_success, on_error):
                on_success(condition_id, "0xtxhash")

            mock_redeem.side_effect = succeed_second
            await manager.attempt_redemption("token1", 100.0)

        await asyncio.sleep(0.1)

        # Should be completed
        assert manager.is_redemption_complete("token1")
        state = manager.get_state("token1")
        assert state.state == RedemptionState.COMPLETED
        assert state.check_count == 2
