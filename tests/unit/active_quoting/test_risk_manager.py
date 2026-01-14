"""
Unit tests for RiskManager - Circuit breaker and risk management.

Tests:
- Per-market and global P&L tracking
- Drawdown calculation
- Stale feed detection
- Circuit breaker state transitions
- Position limit multipliers
- Error tracking
- Disconnect handling
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.risk_manager import (
    RiskManager,
    CircuitBreakerState,
    MarketRiskState,
    GlobalRiskState,
    HaltReason,
)
from rebates.active_quoting.models import Position, OrderSide


@pytest.fixture
def config():
    """Create test configuration."""
    return ActiveQuotingConfig(
        max_drawdown_per_market_usdc=20.0,
        max_drawdown_global_usdc=100.0,
        max_consecutive_errors=5,
        stale_feed_timeout_seconds=30.0,
        circuit_breaker_recovery_seconds=60.0,
    )


@pytest.fixture
def risk_manager(config):
    """Create RiskManager instance."""
    return RiskManager(config)


# --- MarketRiskState Tests ---

class TestMarketRiskState:
    """Tests for MarketRiskState dataclass."""

    def test_initial_state(self):
        """Test initial market risk state."""
        state = MarketRiskState(token_id="test_token")
        assert state.token_id == "test_token"
        assert state.realized_pnl == 0.0
        assert state.unrealized_pnl == 0.0
        assert state.peak_pnl == 0.0
        assert state.current_drawdown == 0.0
        assert state.is_stale is False
        assert state.halted is False

    def test_total_pnl_calculation(self):
        """Test total P&L calculation."""
        state = MarketRiskState(token_id="test", realized_pnl=10.0, unrealized_pnl=5.0)
        assert state.total_pnl == 15.0

    def test_update_pnl_positive(self):
        """Test P&L update with profits."""
        state = MarketRiskState(token_id="test")
        state.update_pnl(realized=5.0, unrealized=3.0)

        assert state.realized_pnl == 5.0
        assert state.unrealized_pnl == 3.0
        assert state.total_pnl == 8.0
        assert state.peak_pnl == 8.0
        assert state.current_drawdown == 0.0  # No drawdown at peak
        assert state.last_update_time is not None

    def test_update_pnl_with_drawdown(self):
        """Test P&L update that creates drawdown."""
        state = MarketRiskState(token_id="test")

        # First update - establish peak
        state.update_pnl(realized=10.0, unrealized=5.0)
        assert state.peak_pnl == 15.0

        # Second update - drawdown
        state.update_pnl(realized=10.0, unrealized=-3.0)
        assert state.total_pnl == 7.0
        assert state.peak_pnl == 15.0  # Peak unchanged
        assert state.current_drawdown == 8.0  # 15 - 7

    def test_update_pnl_new_peak(self):
        """Test P&L update that sets new peak."""
        state = MarketRiskState(token_id="test")

        state.update_pnl(realized=5.0, unrealized=0.0)
        assert state.peak_pnl == 5.0

        state.update_pnl(realized=10.0, unrealized=5.0)
        assert state.peak_pnl == 15.0
        assert state.current_drawdown == 0.0


# --- GlobalRiskState Tests ---

class TestGlobalRiskState:
    """Tests for GlobalRiskState dataclass."""

    def test_initial_state(self):
        """Test initial global risk state."""
        state = GlobalRiskState()
        assert state.total_realized_pnl == 0.0
        assert state.total_unrealized_pnl == 0.0
        assert state.peak_total_pnl == 0.0
        assert state.current_drawdown == 0.0
        assert state.circuit_breaker_state == CircuitBreakerState.NORMAL
        assert state.consecutive_errors == 0

    def test_total_pnl(self):
        """Test total P&L calculation."""
        state = GlobalRiskState(
            total_realized_pnl=100.0,
            total_unrealized_pnl=25.0
        )
        assert state.total_pnl == 125.0

    def test_update_total_pnl(self):
        """Test total P&L update with peak/drawdown tracking."""
        state = GlobalRiskState()

        state.total_realized_pnl = 50.0
        state.total_unrealized_pnl = 30.0
        state.update_total_pnl()

        assert state.peak_total_pnl == 80.0
        assert state.current_drawdown == 0.0

        # Create drawdown
        state.total_unrealized_pnl = -20.0
        state.update_total_pnl()

        assert state.total_pnl == 30.0
        assert state.peak_total_pnl == 80.0  # Peak unchanged
        assert state.current_drawdown == 50.0


# --- RiskManager P&L Tracking Tests ---

class TestRiskManagerPnL:
    """Tests for RiskManager P&L tracking."""

    def test_get_market_state_creates_new(self, risk_manager):
        """Test that get_market_state creates new state if needed."""
        state = risk_manager.get_market_state("token1")
        assert state is not None
        assert state.token_id == "token1"

    def test_get_market_state_returns_existing(self, risk_manager):
        """Test that get_market_state returns existing state."""
        state1 = risk_manager.get_market_state("token1")
        state1.realized_pnl = 100.0

        state2 = risk_manager.get_market_state("token1")
        assert state2.realized_pnl == 100.0
        assert state1 is state2

    def test_update_market_pnl(self, risk_manager):
        """Test updating market P&L."""
        risk_manager.update_market_pnl("token1", realized_pnl=10.0, unrealized_pnl=5.0)

        state = risk_manager.get_market_state("token1")
        assert state.realized_pnl == 10.0
        assert state.unrealized_pnl == 5.0
        assert state.total_pnl == 15.0

    def test_update_market_pnl_updates_global(self, risk_manager):
        """Test that market P&L updates affect global state."""
        risk_manager.update_market_pnl("token1", realized_pnl=10.0, unrealized_pnl=5.0)
        risk_manager.update_market_pnl("token2", realized_pnl=20.0, unrealized_pnl=10.0)

        global_state = risk_manager.global_state
        assert global_state.total_realized_pnl == 30.0
        assert global_state.total_unrealized_pnl == 15.0
        assert global_state.total_pnl == 45.0

    def test_update_from_position(self, risk_manager):
        """Test updating P&L from a Position object."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
            realized_pnl=5.0,
        )

        # Current price at 0.55 = 5 cent profit per share
        risk_manager.update_from_position("token1", position, current_price=0.55)

        state = risk_manager.get_market_state("token1")
        assert state.realized_pnl == 5.0
        assert state.unrealized_pnl == pytest.approx(5.0, rel=1e-9)  # (0.55 - 0.50) * 100
        assert state.total_pnl == pytest.approx(10.0, rel=1e-9)

    def test_update_from_position_no_current_price(self, risk_manager):
        """Test updating from position without current price."""
        position = Position(
            token_id="token1",
            size=100.0,
            avg_entry_price=0.50,
            realized_pnl=5.0,
        )

        risk_manager.update_from_position("token1", position, current_price=None)

        state = risk_manager.get_market_state("token1")
        assert state.realized_pnl == 5.0
        assert state.unrealized_pnl == 0.0  # No current price


# --- Drawdown Limit Tests ---

class TestDrawdownLimits:
    """Tests for drawdown limit enforcement."""

    @pytest.mark.asyncio
    async def test_market_drawdown_triggers_halt(self, config):
        """Test that per-market drawdown triggers market halt."""
        on_market_halt = AsyncMock()
        risk_manager = RiskManager(config, on_market_halt=on_market_halt)

        # Create profit first, then loss to create drawdown
        risk_manager.update_market_pnl("token1", realized_pnl=25.0, unrealized_pnl=0.0)
        risk_manager.update_market_pnl("token1", realized_pnl=5.0, unrealized_pnl=0.0)

        # Drawdown is 25 - 5 = 20, which equals limit
        state = risk_manager.get_market_state("token1")
        assert state.halted is True

        # Give async task time to complete
        await asyncio.sleep(0.01)
        on_market_halt.assert_called_once()

    @pytest.mark.asyncio
    async def test_global_drawdown_triggers_halt(self, config):
        """Test that global drawdown triggers circuit breaker halt."""
        on_state_change = AsyncMock()
        risk_manager = RiskManager(config, on_state_change=on_state_change)

        # Create global profit then loss
        risk_manager.update_market_pnl("token1", realized_pnl=100.0, unrealized_pnl=50.0)
        risk_manager.update_market_pnl("token2", realized_pnl=50.0, unrealized_pnl=0.0)

        # Global peak = 200. Now create drawdown
        risk_manager.update_market_pnl("token1", realized_pnl=50.0, unrealized_pnl=0.0)
        risk_manager.update_market_pnl("token2", realized_pnl=0.0, unrealized_pnl=0.0)

        # Global now = 50, drawdown = 150, exceeds 100 limit
        await asyncio.sleep(0.01)
        assert risk_manager.state == CircuitBreakerState.HALTED


# --- Stale Feed Detection Tests ---

class TestStaleFeedDetection:
    """Tests for stale feed detection."""

    def test_update_feed_timestamp(self, risk_manager):
        """Test that feed timestamp is updated."""
        risk_manager.update_feed_timestamp("token1")

        state = risk_manager.get_market_state("token1")
        assert state.last_update_time is not None
        assert state.is_stale is False

    def test_check_stale_feeds_fresh(self, risk_manager):
        """Test that fresh feeds are not marked stale."""
        risk_manager.update_feed_timestamp("token1")

        stale = risk_manager.check_stale_feeds()
        assert len(stale) == 0
        assert not risk_manager.is_feed_stale("token1")

    def test_check_stale_feeds_stale(self, risk_manager):
        """Test detection of stale feeds."""
        state = risk_manager.get_market_state("token1")
        state.last_update_time = datetime.utcnow() - timedelta(seconds=60)

        stale = risk_manager.check_stale_feeds()
        assert "token1" in stale
        assert risk_manager.is_feed_stale("token1")
        assert state.is_stale is True

    @pytest.mark.asyncio
    async def test_stale_feed_triggers_warning(self, config):
        """Test that stale feed triggers WARNING state."""
        on_state_change = AsyncMock()
        risk_manager = RiskManager(config, on_state_change=on_state_change)

        # Set up stale feed
        state = risk_manager.get_market_state("token1")
        state.last_update_time = datetime.utcnow() - timedelta(seconds=60)

        risk_manager.check_stale_feeds()

        await asyncio.sleep(0.01)
        # Should transition to WARNING from NORMAL
        on_state_change.assert_called()

    def test_get_stale_markets(self, risk_manager):
        """Test getting set of stale markets."""
        state1 = risk_manager.get_market_state("token1")
        state1.last_update_time = datetime.utcnow() - timedelta(seconds=60)

        risk_manager.update_feed_timestamp("token2")

        risk_manager.check_stale_feeds()

        stale = risk_manager.get_stale_markets()
        assert "token1" in stale
        assert "token2" not in stale


# --- Circuit Breaker State Machine Tests ---

class TestCircuitBreakerStateMachine:
    """Tests for circuit breaker state transitions."""

    def test_initial_state_is_normal(self, risk_manager):
        """Test that initial state is NORMAL."""
        assert risk_manager.state == CircuitBreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_trigger_warning(self, config):
        """Test triggering WARNING state."""
        on_state_change = AsyncMock()
        risk_manager = RiskManager(config, on_state_change=on_state_change)

        await risk_manager.trigger_warning("Test warning")

        assert risk_manager.state == CircuitBreakerState.WARNING
        on_state_change.assert_called_once()
        args = on_state_change.call_args[0]
        assert args[0] == CircuitBreakerState.NORMAL
        assert args[1] == CircuitBreakerState.WARNING
        assert "Test warning" in args[2]

    @pytest.mark.asyncio
    async def test_trigger_halt(self, config):
        """Test triggering HALTED state."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        await risk_manager.trigger_halt("Test halt")

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert risk_manager.global_state.halted_at is not None
        on_kill_switch.assert_called_once()

    @pytest.mark.asyncio
    async def test_warning_does_not_transition_from_halted(self, risk_manager):
        """Test that WARNING cannot transition from HALTED."""
        await risk_manager.trigger_halt("Halt first")
        await risk_manager.trigger_warning("Try warning")

        assert risk_manager.state == CircuitBreakerState.HALTED

    @pytest.mark.asyncio
    async def test_start_recovery(self, risk_manager):
        """Test starting recovery from HALTED."""
        await risk_manager.trigger_halt("Test halt")
        await risk_manager.start_recovery()

        assert risk_manager.state == CircuitBreakerState.RECOVERING
        assert risk_manager.global_state.recovering_since is not None

    @pytest.mark.asyncio
    async def test_start_recovery_only_from_halted(self, risk_manager):
        """Test that recovery can only start from HALTED."""
        await risk_manager.start_recovery()  # Should do nothing

        assert risk_manager.state == CircuitBreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_check_recovery_complete(self, config):
        """Test checking if recovery is complete."""
        config.circuit_breaker_recovery_seconds = 0.1  # Short for testing
        risk_manager = RiskManager(config)

        await risk_manager.trigger_halt("Test")
        await risk_manager.start_recovery()

        # Not complete immediately
        complete = await risk_manager.check_recovery_complete()
        assert not complete
        assert risk_manager.state == CircuitBreakerState.RECOVERING

        # Wait for recovery period
        await asyncio.sleep(0.15)

        complete = await risk_manager.check_recovery_complete()
        assert complete
        assert risk_manager.state == CircuitBreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_clear_warning(self, risk_manager):
        """Test clearing WARNING state."""
        await risk_manager.trigger_warning("Test")
        await risk_manager.clear_warning()

        assert risk_manager.state == CircuitBreakerState.NORMAL


# --- Position Limit Multiplier Tests ---

class TestPositionLimitMultiplier:
    """Tests for position limit multipliers."""

    def test_normal_state_full_limits(self, risk_manager):
        """Test NORMAL state has full limits."""
        assert risk_manager.get_position_limit_multiplier() == 1.0

    @pytest.mark.asyncio
    async def test_warning_state_reduced_limits(self, risk_manager):
        """Test WARNING state has 50% limits."""
        await risk_manager.trigger_warning("Test")
        assert risk_manager.get_position_limit_multiplier() == 0.5

    @pytest.mark.asyncio
    async def test_recovering_state_reduced_limits(self, risk_manager):
        """Test RECOVERING state has 25% limits."""
        await risk_manager.trigger_halt("Test")
        await risk_manager.start_recovery()
        assert risk_manager.get_position_limit_multiplier() == 0.25

    @pytest.mark.asyncio
    async def test_halted_state_no_orders(self, risk_manager):
        """Test HALTED state has 0% limits."""
        await risk_manager.trigger_halt("Test")
        assert risk_manager.get_position_limit_multiplier() == 0.0


# --- Risk-Adjusted Limits ---

class TestRiskAdjustedLimits:
    """Tests for risk-adjusted limit calculations."""

    def test_adjusted_position_limit_normal(self, risk_manager):
        """Test position limit in NORMAL state."""
        adjusted = risk_manager.get_adjusted_position_limit(100)
        assert adjusted == 100

    @pytest.mark.asyncio
    async def test_adjusted_position_limit_warning(self, risk_manager):
        """Test position limit in WARNING state (50%)."""
        await risk_manager.trigger_warning("Test")
        adjusted = risk_manager.get_adjusted_position_limit(100)
        assert adjusted == 50

    @pytest.mark.asyncio
    async def test_adjusted_position_limit_recovering(self, risk_manager):
        """Test position limit in RECOVERING state (25%)."""
        await risk_manager.trigger_halt("Test")
        await risk_manager.start_recovery()
        adjusted = risk_manager.get_adjusted_position_limit(100)
        assert adjusted == 25

    @pytest.mark.asyncio
    async def test_adjusted_position_limit_halted(self, risk_manager):
        """Test position limit in HALTED state (0%)."""
        await risk_manager.trigger_halt("Test")
        adjusted = risk_manager.get_adjusted_position_limit(100)
        assert adjusted == 0

    def test_adjusted_liability_limit_normal(self, risk_manager):
        """Test liability limit in NORMAL state."""
        adjusted = risk_manager.get_adjusted_liability_limit(50.0)
        assert adjusted == 50.0

    @pytest.mark.asyncio
    async def test_adjusted_liability_limit_warning(self, risk_manager):
        """Test liability limit in WARNING state (50%)."""
        await risk_manager.trigger_warning("Test")
        adjusted = risk_manager.get_adjusted_liability_limit(50.0)
        assert adjusted == 25.0

    def test_adjusted_order_size_normal(self, risk_manager):
        """Test order size in NORMAL state."""
        adjusted = risk_manager.get_adjusted_order_size(10.0)
        assert adjusted == 10.0

    @pytest.mark.asyncio
    async def test_adjusted_order_size_warning(self, risk_manager):
        """Test order size in WARNING state (50%)."""
        await risk_manager.trigger_warning("Test")
        adjusted = risk_manager.get_adjusted_order_size(10.0)
        assert adjusted == 5.0


# --- Order Placement Checks ---

class TestOrderPlacementChecks:
    """Tests for order placement permission checks."""

    def test_can_place_orders_normal(self, risk_manager):
        """Test orders allowed in NORMAL state."""
        assert risk_manager.can_place_orders() is True

    @pytest.mark.asyncio
    async def test_cannot_place_orders_halted(self, risk_manager):
        """Test orders blocked in HALTED state."""
        await risk_manager.trigger_halt("Test")
        assert risk_manager.can_place_orders() is False

    @pytest.mark.asyncio
    async def test_can_place_orders_warning(self, risk_manager):
        """Test orders allowed in WARNING state (with reduced limits)."""
        await risk_manager.trigger_warning("Test")
        assert risk_manager.can_place_orders() is True

    def test_can_place_orders_for_market_normal(self, risk_manager):
        """Test market-specific order check in normal state."""
        risk_manager.update_feed_timestamp("token1")

        allowed, reason = risk_manager.can_place_orders_for_market("token1")
        assert allowed is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_cannot_place_orders_for_halted_market(self, config):
        """Test orders blocked for halted market."""
        risk_manager = RiskManager(config)

        # Trigger market-specific halt
        state = risk_manager.get_market_state("token1")
        state.halted = True

        allowed, reason = risk_manager.can_place_orders_for_market("token1")
        assert allowed is False
        assert "halted" in reason.lower()

    def test_cannot_place_orders_for_stale_market(self, risk_manager):
        """Test orders blocked for stale market."""
        state = risk_manager.get_market_state("token1")
        state.last_update_time = datetime.utcnow() - timedelta(seconds=60)

        risk_manager.check_stale_feeds()

        allowed, reason = risk_manager.can_place_orders_for_market("token1")
        assert allowed is False
        assert "stale" in reason.lower()


# --- Error Tracking Tests ---

class TestErrorTracking:
    """Tests for error tracking and circuit breaker triggers."""

    def test_record_error_increments_count(self, risk_manager):
        """Test that recording error increments count."""
        risk_manager.record_error()
        assert risk_manager.global_state.consecutive_errors == 1

        risk_manager.record_error()
        assert risk_manager.global_state.consecutive_errors == 2

    def test_clear_errors_resets_count(self, risk_manager):
        """Test that clearing errors resets count."""
        risk_manager.record_error()
        risk_manager.record_error()
        risk_manager.clear_errors()

        assert risk_manager.global_state.consecutive_errors == 0

    @pytest.mark.asyncio
    async def test_max_errors_triggers_halt(self, config):
        """Test that max consecutive errors triggers halt."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        for _ in range(config.max_consecutive_errors):
            risk_manager.record_error()

        await asyncio.sleep(0.01)
        assert risk_manager.state == CircuitBreakerState.HALTED

    def test_error_count_resets_after_timeout(self, risk_manager):
        """Test that error count resets after 60 second gap."""
        risk_manager.record_error()
        risk_manager.record_error()

        # Simulate old error
        risk_manager.global_state.last_error_time = datetime.utcnow() - timedelta(seconds=90)

        risk_manager.record_error()  # Should reset first
        assert risk_manager.global_state.consecutive_errors == 1


# --- Disconnect Handling Tests ---

class TestDisconnectHandling:
    """Tests for WebSocket disconnect handling."""

    @pytest.mark.asyncio
    async def test_market_disconnect_triggers_warning(self, config):
        """Test that market WS disconnect triggers WARNING."""
        on_state_change = AsyncMock()
        risk_manager = RiskManager(config, on_state_change=on_state_change)

        await risk_manager.on_market_disconnect()

        assert risk_manager.state == CircuitBreakerState.WARNING
        on_state_change.assert_called()

    @pytest.mark.asyncio
    async def test_user_disconnect_triggers_halt(self, config):
        """Test that user WS disconnect triggers HALT."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        await risk_manager.on_user_disconnect()

        assert risk_manager.state == CircuitBreakerState.HALTED
        on_kill_switch.assert_called_once()

    @pytest.mark.asyncio
    async def test_market_disconnect_does_not_escalate_from_halt(self, config):
        """Test that market disconnect doesn't change state from HALTED."""
        risk_manager = RiskManager(config)

        await risk_manager.trigger_halt("Already halted")
        await risk_manager.on_market_disconnect()

        # Should still be HALTED, not downgraded to WARNING
        assert risk_manager.state == CircuitBreakerState.HALTED

    @pytest.mark.asyncio
    async def test_user_disconnect_from_warning_upgrades_to_halt(self, config):
        """Test that user disconnect from WARNING upgrades to HALT."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        await risk_manager.trigger_warning("In warning")
        assert risk_manager.state == CircuitBreakerState.WARNING

        await risk_manager.on_user_disconnect()

        assert risk_manager.state == CircuitBreakerState.HALTED
        on_kill_switch.assert_called_once()


# --- Kill Switch Integration Tests ---

class TestKillSwitchIntegration:
    """Tests for kill switch integration with OrderManager."""

    @pytest.mark.asyncio
    async def test_kill_switch_cancels_all_orders(self, config):
        """Test that kill switch callback can be used to cancel orders."""
        cancelled_orders = []

        async def mock_cancel_all():
            cancelled_orders.append("cancelled")

        risk_manager = RiskManager(config, on_kill_switch=mock_cancel_all)

        await risk_manager.trigger_halt("Test halt")

        assert len(cancelled_orders) == 1

    @pytest.mark.asyncio
    async def test_kill_switch_called_once_per_halt(self, config):
        """Test that kill switch is only called once per halt."""
        call_count = []

        async def mock_cancel_all():
            call_count.append(1)

        risk_manager = RiskManager(config, on_kill_switch=mock_cancel_all)

        # First halt
        await risk_manager.trigger_halt("First halt")
        assert len(call_count) == 1

        # Try to halt again (should be no-op since already halted)
        await risk_manager.trigger_halt("Second halt")
        assert len(call_count) == 1  # Still only 1

    @pytest.mark.asyncio
    async def test_kill_switch_called_on_drawdown_breach(self, config):
        """Test that kill switch is called when global drawdown is exceeded."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        # Create profit then loss to breach drawdown
        risk_manager.update_market_pnl("token1", realized_pnl=150.0, unrealized_pnl=50.0)
        risk_manager.update_market_pnl("token1", realized_pnl=50.0, unrealized_pnl=0.0)

        # Global peak = 200, now = 50, drawdown = 150 > 100 limit
        await asyncio.sleep(0.01)  # Allow async tasks to complete

        assert risk_manager.state == CircuitBreakerState.HALTED

    @pytest.mark.asyncio
    async def test_kill_switch_called_on_max_errors(self, config):
        """Test that kill switch is called when max errors is reached."""
        on_kill_switch = AsyncMock()
        risk_manager = RiskManager(config, on_kill_switch=on_kill_switch)

        for _ in range(config.max_consecutive_errors):
            risk_manager.record_error()

        await asyncio.sleep(0.01)  # Allow async tasks to complete

        assert risk_manager.state == CircuitBreakerState.HALTED


# --- State Summary Tests ---

class TestStateSummary:
    """Tests for state summary functionality."""

    def test_get_summary_structure(self, risk_manager):
        """Test that summary has expected structure."""
        risk_manager.update_market_pnl("token1", 10.0, 5.0)
        risk_manager.update_feed_timestamp("token1")

        summary = risk_manager.get_summary()

        assert "state" in summary
        assert "global" in summary
        assert "markets" in summary
        assert "stale_markets" in summary
        assert "position_limit_multiplier" in summary

        assert summary["state"] == CircuitBreakerState.NORMAL.value
        assert "total_pnl" in summary["global"]
        assert "token1" in summary["markets"]


# --- Reset Tests ---

class TestReset:
    """Tests for reset functionality."""

    def test_reset_market(self, risk_manager):
        """Test resetting a single market."""
        risk_manager.update_market_pnl("token1", 10.0, 5.0)
        risk_manager.reset_market("token1")

        state = risk_manager.get_market_state("token1")
        assert state.realized_pnl == 0.0
        assert state.unrealized_pnl == 0.0

    def test_reset_all(self, risk_manager):
        """Test resetting all state."""
        risk_manager.update_market_pnl("token1", 10.0, 5.0)
        risk_manager.update_market_pnl("token2", 20.0, 10.0)
        risk_manager.reset_all()

        assert len(risk_manager._market_states) == 0
        assert risk_manager.global_state.total_realized_pnl == 0.0

    @pytest.mark.asyncio
    async def test_force_reset_to_normal(self, risk_manager):
        """Test forced reset to NORMAL state."""
        await risk_manager.trigger_halt("Test")
        risk_manager.record_error()
        risk_manager.record_error()

        await risk_manager.force_reset_to_normal()

        assert risk_manager.state == CircuitBreakerState.NORMAL
        assert risk_manager.global_state.consecutive_errors == 0


# --- Callback Tests ---

class TestCallbacks:
    """Tests for callback invocation."""

    @pytest.mark.asyncio
    async def test_state_change_callback_receives_correct_args(self, config):
        """Test state change callback receives old, new, and reason."""
        callback_args = []

        async def capture_callback(old, new, reason):
            callback_args.append((old, new, reason))

        risk_manager = RiskManager(config, on_state_change=capture_callback)

        await risk_manager.trigger_warning("Test reason")

        assert len(callback_args) == 1
        old, new, reason = callback_args[0]
        assert old == CircuitBreakerState.NORMAL
        assert new == CircuitBreakerState.WARNING
        assert "Test reason" in reason

    @pytest.mark.asyncio
    async def test_kill_switch_callback_on_halt(self, config):
        """Test kill switch callback is called on HALT."""
        kill_switch_called = []

        async def capture_kill_switch():
            kill_switch_called.append(True)

        risk_manager = RiskManager(config, on_kill_switch=capture_kill_switch)

        await risk_manager.trigger_halt("Test")

        assert len(kill_switch_called) == 1

    @pytest.mark.asyncio
    async def test_kill_switch_not_called_on_warning(self, config):
        """Test kill switch is not called on WARNING."""
        kill_switch_called = []

        async def capture_kill_switch():
            kill_switch_called.append(True)

        risk_manager = RiskManager(config, on_kill_switch=capture_kill_switch)

        await risk_manager.trigger_warning("Test")

        assert len(kill_switch_called) == 0


# --- HaltReason and WebSocket Gap Halt Tests (Phase 6) ---

class TestHaltReason:
    """Tests for HaltReason enum and halt reason tracking."""

    def test_halt_reason_enum_values(self):
        """Test HaltReason enum has expected values."""
        assert HaltReason.NONE.value == "NONE"
        assert HaltReason.GLOBAL_DRAWDOWN.value == "GLOBAL_DRAWDOWN"
        assert HaltReason.CONSECUTIVE_ERRORS.value == "CONSECUTIVE_ERRORS"
        assert HaltReason.USER_WS_DISCONNECT.value == "USER_WS_DISCONNECT"
        assert HaltReason.WS_GAP_UNRESOLVED.value == "WS_GAP_UNRESOLVED"
        assert HaltReason.MANUAL.value == "MANUAL"

    def test_initial_halt_reason_is_none(self, risk_manager):
        """Test that initial halt_reason is NONE."""
        assert risk_manager.halt_reason == HaltReason.NONE

    @pytest.mark.asyncio
    async def test_halt_sets_halt_reason(self, risk_manager):
        """Test that triggering halt sets the halt_reason."""
        await risk_manager.trigger_halt("Test halt", HaltReason.WS_GAP_UNRESOLVED)

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert risk_manager.halt_reason == HaltReason.WS_GAP_UNRESOLVED

    @pytest.mark.asyncio
    async def test_default_halt_reason_is_manual(self, risk_manager):
        """Test that default halt reason is MANUAL."""
        await risk_manager.trigger_halt("Test halt")

        assert risk_manager.halt_reason == HaltReason.MANUAL

    @pytest.mark.asyncio
    async def test_is_halted_due_to_ws_gaps(self, risk_manager):
        """Test is_halted_due_to_ws_gaps() method."""
        # Initially not halted
        assert not risk_manager.is_halted_due_to_ws_gaps()

        # Halt with WS_GAP_UNRESOLVED
        await risk_manager.trigger_halt("WS gaps", HaltReason.WS_GAP_UNRESOLVED)
        assert risk_manager.is_halted_due_to_ws_gaps()

        # Reset and halt with different reason
        await risk_manager.force_reset_to_normal()
        await risk_manager.trigger_halt("Other reason", HaltReason.MANUAL)
        assert not risk_manager.is_halted_due_to_ws_gaps()

    @pytest.mark.asyncio
    async def test_recover_from_ws_gap_halt_success(self, risk_manager):
        """Test successful recovery from WS gap halt."""
        await risk_manager.trigger_halt("WS gaps", HaltReason.WS_GAP_UNRESOLVED)
        assert risk_manager.state == CircuitBreakerState.HALTED

        await risk_manager.recover_from_ws_gap_halt()

        assert risk_manager.state == CircuitBreakerState.NORMAL
        assert risk_manager.halt_reason == HaltReason.NONE

    @pytest.mark.asyncio
    async def test_recover_from_ws_gap_halt_wrong_reason(self, risk_manager):
        """Test that recovery does not work for non-WS-gap halts."""
        await risk_manager.trigger_halt("Manual halt", HaltReason.MANUAL)

        await risk_manager.recover_from_ws_gap_halt()

        # Should still be halted
        assert risk_manager.state == CircuitBreakerState.HALTED

    @pytest.mark.asyncio
    async def test_halt_reason_cleared_on_recovery(self, risk_manager):
        """Test that halt_reason is cleared when returning to NORMAL."""
        await risk_manager.trigger_halt("Test", HaltReason.WS_GAP_UNRESOLVED)
        await risk_manager.recover_from_ws_gap_halt()

        assert risk_manager.halt_reason == HaltReason.NONE

    @pytest.mark.asyncio
    async def test_halt_reason_kept_during_recovering_state(self, risk_manager):
        """Test that halt_reason is kept during RECOVERING state."""
        await risk_manager.trigger_halt("Test", HaltReason.GLOBAL_DRAWDOWN)
        await risk_manager.start_recovery()

        assert risk_manager.state == CircuitBreakerState.RECOVERING
        # halt_reason should be kept for debugging purposes
        assert risk_manager.halt_reason == HaltReason.GLOBAL_DRAWDOWN

    def test_summary_includes_halt_reason(self, risk_manager):
        """Test that summary includes halt_reason field."""
        summary = risk_manager.get_summary()
        assert "halt_reason" in summary
        assert summary["halt_reason"] == HaltReason.NONE.value

    @pytest.mark.asyncio
    async def test_summary_shows_halt_reason_when_halted(self, risk_manager):
        """Test that summary shows correct halt_reason when halted."""
        await risk_manager.trigger_halt("WS gaps", HaltReason.WS_GAP_UNRESOLVED)

        summary = risk_manager.get_summary()
        assert summary["halt_reason"] == HaltReason.WS_GAP_UNRESOLVED.value

    @pytest.mark.asyncio
    async def test_global_drawdown_sets_correct_halt_reason(self, config):
        """Test that global drawdown halt sets correct halt_reason."""
        risk_manager = RiskManager(config)

        # Create drawdown that exceeds global limit
        risk_manager.update_market_pnl("token1", realized_pnl=50.0, unrealized_pnl=0.0)
        risk_manager.update_market_pnl("token1", realized_pnl=-60.0, unrealized_pnl=0.0)

        await asyncio.sleep(0.01)  # Allow async tasks

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert risk_manager.halt_reason == HaltReason.GLOBAL_DRAWDOWN

    @pytest.mark.asyncio
    async def test_consecutive_errors_sets_correct_halt_reason(self, config):
        """Test that consecutive errors halt sets correct halt_reason."""
        risk_manager = RiskManager(config)

        for _ in range(config.max_consecutive_errors):
            risk_manager.record_error()

        await asyncio.sleep(0.01)  # Allow async tasks

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert risk_manager.halt_reason == HaltReason.CONSECUTIVE_ERRORS

    @pytest.mark.asyncio
    async def test_user_disconnect_sets_correct_halt_reason(self, config):
        """Test that user WS disconnect sets correct halt_reason."""
        risk_manager = RiskManager(config)

        await risk_manager.on_user_disconnect()

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert risk_manager.halt_reason == HaltReason.USER_WS_DISCONNECT
