"""
Integration tests for Phase 4: Risk Management + Circuit Breaker.

Tests:
- Verify circuit breaker triggers on simulated drawdown
- Verify orders cancelled when circuit breaker halts
- Simulate WebSocket disconnect, verify kill switch fires
- Test recovery from halted state

Run with: POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_risk_management.py -v
"""
import asyncio
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from rebates.active_quoting.config import ActiveQuotingConfig
from rebates.active_quoting.risk_manager import (
    RiskManager,
    CircuitBreakerState,
)
from rebates.active_quoting.order_manager import OrderManager
from rebates.active_quoting.orderbook_manager import OrderbookManager
from rebates.active_quoting.user_channel_manager import UserChannelManager
from rebates.active_quoting.inventory_manager import InventoryManager
from rebates.active_quoting.models import Position, OrderSide, Fill, OrderState, OrderStatus


# Skip all tests if POLY_TEST_INTEGRATION is not set
pytestmark = pytest.mark.skipif(
    os.getenv("POLY_TEST_INTEGRATION", "").lower() != "true",
    reason="Integration tests require POLY_TEST_INTEGRATION=true"
)


@pytest.fixture
def config():
    """Create test configuration with short timeouts for testing."""
    return ActiveQuotingConfig(
        max_drawdown_per_market_usdc=20.0,
        max_drawdown_global_usdc=100.0,
        max_consecutive_errors=5,
        stale_feed_timeout_seconds=5.0,  # Short for testing
        circuit_breaker_recovery_seconds=2.0,  # Short for testing
        dry_run=True,  # Always dry run for integration tests
    )


@pytest.fixture
def order_manager(config):
    """Create OrderManager in dry-run mode."""
    return OrderManager(
        config=config,
        api_key="test_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
    )


@pytest.fixture
def inventory_manager(config):
    """Create InventoryManager."""
    return InventoryManager(config)


class TestCircuitBreakerDrawdown:
    """Test circuit breaker triggers on drawdown."""

    @pytest.mark.asyncio
    async def test_per_market_drawdown_halts_market(self, config):
        """Verify per-market drawdown triggers market halt."""
        market_halt_calls = []

        async def on_market_halt(token_id: str, reason: str):
            market_halt_calls.append((token_id, reason))

        risk_manager = RiskManager(
            config=config,
            on_market_halt=on_market_halt,
        )

        # Establish peak P&L
        risk_manager.update_market_pnl("token1", realized_pnl=30.0, unrealized_pnl=0.0)
        assert risk_manager.get_market_state("token1").peak_pnl == 30.0

        # Create drawdown exceeding limit (20 USDC)
        risk_manager.update_market_pnl("token1", realized_pnl=5.0, unrealized_pnl=0.0)

        # Wait for async callbacks
        await asyncio.sleep(0.05)

        # Market should be halted
        market_state = risk_manager.get_market_state("token1")
        assert market_state.halted is True
        assert market_state.current_drawdown >= config.max_drawdown_per_market_usdc

        # Callback should have been called
        assert len(market_halt_calls) == 1
        assert market_halt_calls[0][0] == "token1"
        assert "drawdown" in market_halt_calls[0][1].lower()

        # Verify orders can't be placed for this market
        allowed, reason = risk_manager.can_place_orders_for_market("token1")
        assert allowed is False
        assert "halted" in reason.lower()

    @pytest.mark.asyncio
    async def test_global_drawdown_triggers_full_halt(self, config):
        """Verify global drawdown triggers circuit breaker HALT."""
        state_changes = []
        kill_switch_calls = []

        async def on_state_change(old, new, reason):
            state_changes.append((old, new, reason))

        async def on_kill_switch():
            kill_switch_calls.append(datetime.utcnow())

        risk_manager = RiskManager(
            config=config,
            on_state_change=on_state_change,
            on_kill_switch=on_kill_switch,
        )

        # Establish high P&L across multiple markets
        risk_manager.update_market_pnl("token1", realized_pnl=100.0, unrealized_pnl=50.0)
        risk_manager.update_market_pnl("token2", realized_pnl=50.0, unrealized_pnl=0.0)

        global_peak = risk_manager.global_state.peak_total_pnl
        assert global_peak == 200.0

        # Create global drawdown exceeding limit (100 USDC)
        risk_manager.update_market_pnl("token1", realized_pnl=50.0, unrealized_pnl=0.0)
        risk_manager.update_market_pnl("token2", realized_pnl=0.0, unrealized_pnl=0.0)

        # Wait for async callbacks
        await asyncio.sleep(0.05)

        # Global drawdown is 200 - 50 = 150, exceeds 100 limit
        assert risk_manager.state == CircuitBreakerState.HALTED
        assert len(kill_switch_calls) >= 1

        # Verify state change callback was called
        assert any(new == CircuitBreakerState.HALTED for old, new, reason in state_changes)


class TestCircuitBreakerOrderCancellation:
    """Test orders are cancelled when circuit breaker halts."""

    @pytest.mark.asyncio
    async def test_orders_cancelled_on_halt(self, config, order_manager):
        """Verify all orders cancelled when circuit breaker triggers HALT."""
        # Track if cancel_all was called
        cancel_all_calls = []
        original_cancel_all = order_manager.cancel_all

        async def mock_cancel_all():
            result = await original_cancel_all()
            cancel_all_calls.append(result)
            return result

        order_manager.cancel_all = mock_cancel_all

        # Set up risk manager with order manager's cancel_all as kill switch
        async def on_kill_switch():
            await order_manager.cancel_all()

        risk_manager = RiskManager(
            config=config,
            on_kill_switch=on_kill_switch,
        )

        # Place some orders (dry run mode)
        await order_manager.place_order("token1", OrderSide.BUY, 0.45, 10.0)
        await order_manager.place_order("token1", OrderSide.SELL, 0.55, 10.0)
        await order_manager.place_order("token2", OrderSide.BUY, 0.48, 10.0)

        # Verify orders are open
        open_orders = order_manager.get_pending_orders()
        assert len(open_orders) == 3

        # Trigger halt
        await risk_manager.trigger_halt("Test halt")

        # Verify cancel_all was called
        assert len(cancel_all_calls) >= 1

        # Verify orders are cancelled (in dry run mode, they get marked cancelled)
        open_orders_after = [o for o in order_manager.get_pending_orders() if o.is_open()]
        assert len(open_orders_after) == 0

    @pytest.mark.asyncio
    async def test_position_limits_reduced_on_warning(self, config):
        """Verify position limits are reduced to 50% on WARNING."""
        risk_manager = RiskManager(config=config)

        base_limit = config.max_position_per_market

        # In NORMAL state, full limits
        assert risk_manager.get_adjusted_position_limit(base_limit) == base_limit

        # Trigger WARNING
        await risk_manager.trigger_warning("Test warning")

        # In WARNING state, 50% limits
        expected = int(base_limit * 0.5)
        assert risk_manager.get_adjusted_position_limit(base_limit) == expected

    @pytest.mark.asyncio
    async def test_position_limits_reduced_on_recovering(self, config):
        """Verify position limits are reduced to 25% on RECOVERING."""
        risk_manager = RiskManager(config=config)

        base_limit = config.max_position_per_market

        # Trigger HALT then start recovery
        await risk_manager.trigger_halt("Test halt")
        await risk_manager.start_recovery()

        # In RECOVERING state, 25% limits
        expected = int(base_limit * 0.25)
        assert risk_manager.get_adjusted_position_limit(base_limit) == expected


class TestWebSocketDisconnectHandling:
    """Test WebSocket disconnect triggers circuit breaker."""

    @pytest.mark.asyncio
    async def test_market_ws_disconnect_triggers_warning(self, config):
        """Verify market WebSocket disconnect triggers WARNING."""
        state_changes = []

        async def on_state_change(old, new, reason):
            state_changes.append((old, new, reason))

        risk_manager = RiskManager(
            config=config,
            on_state_change=on_state_change,
        )

        # Simulate market WS disconnect
        await risk_manager.on_market_disconnect()

        assert risk_manager.state == CircuitBreakerState.WARNING

        # Verify state change recorded
        assert len(state_changes) == 1
        old, new, reason = state_changes[0]
        assert old == CircuitBreakerState.NORMAL
        assert new == CircuitBreakerState.WARNING
        assert "disconnect" in reason.lower()

    @pytest.mark.asyncio
    async def test_user_ws_disconnect_triggers_halt(self, config):
        """Verify user WebSocket disconnect triggers HALT."""
        kill_switch_calls = []

        async def on_kill_switch():
            kill_switch_calls.append(datetime.utcnow())

        risk_manager = RiskManager(
            config=config,
            on_kill_switch=on_kill_switch,
        )

        # Simulate user WS disconnect
        await risk_manager.on_user_disconnect()

        assert risk_manager.state == CircuitBreakerState.HALTED
        assert len(kill_switch_calls) == 1

    @pytest.mark.asyncio
    async def test_integrated_ws_disconnect_flow(self, config, order_manager):
        """Test full flow: WS disconnect -> circuit breaker -> cancel orders."""
        # Track state
        cancelled_tokens = []

        async def track_cancellations():
            result = await order_manager.cancel_all()
            cancelled_tokens.append(result)
            return result

        risk_manager = RiskManager(
            config=config,
            on_kill_switch=track_cancellations,
        )

        # Create OrderbookManager with risk_manager callback
        orderbook_manager = OrderbookManager(
            config=config,
            on_disconnect=risk_manager.on_market_disconnect,
        )

        # Create UserChannelManager with risk_manager callback
        user_channel_manager = UserChannelManager(
            config=config,
            api_key="test",
            api_secret="test",
            api_passphrase="test",
            on_disconnect=risk_manager.on_user_disconnect,
        )

        # Place some orders
        await order_manager.place_order("token1", OrderSide.BUY, 0.45, 10.0)
        await order_manager.place_order("token2", OrderSide.BUY, 0.45, 10.0)

        # Simulate user WS disconnect (critical - triggers HALT)
        await user_channel_manager.on_disconnect()

        # Wait for async processing
        await asyncio.sleep(0.05)

        # Verify HALT triggered and orders cancelled
        assert risk_manager.state == CircuitBreakerState.HALTED
        assert len(cancelled_tokens) >= 1


class TestRecoveryFromHalt:
    """Test recovery from HALTED state."""

    @pytest.mark.asyncio
    async def test_recovery_process(self, config):
        """Test full recovery process from HALT to NORMAL."""
        state_changes = []

        async def on_state_change(old, new, reason):
            state_changes.append((old, new, reason))

        # Use short recovery time for testing
        config.circuit_breaker_recovery_seconds = 0.5
        risk_manager = RiskManager(
            config=config,
            on_state_change=on_state_change,
        )

        # Trigger HALT
        await risk_manager.trigger_halt("Test halt")
        assert risk_manager.state == CircuitBreakerState.HALTED

        # Start recovery
        await risk_manager.start_recovery()
        assert risk_manager.state == CircuitBreakerState.RECOVERING
        assert risk_manager.get_position_limit_multiplier() == 0.25

        # Recovery not complete immediately
        complete = await risk_manager.check_recovery_complete()
        assert not complete

        # Wait for recovery period
        await asyncio.sleep(0.6)

        # Check recovery complete
        complete = await risk_manager.check_recovery_complete()
        assert complete
        assert risk_manager.state == CircuitBreakerState.NORMAL
        assert risk_manager.get_position_limit_multiplier() == 1.0

        # Verify state transitions
        states_seen = [new for old, new, reason in state_changes]
        assert CircuitBreakerState.HALTED in states_seen
        assert CircuitBreakerState.RECOVERING in states_seen
        assert CircuitBreakerState.NORMAL in states_seen

    @pytest.mark.asyncio
    async def test_cannot_recover_until_halted(self, config):
        """Test that recovery can only start from HALTED state."""
        risk_manager = RiskManager(config=config)

        # Try to start recovery from NORMAL
        await risk_manager.start_recovery()
        assert risk_manager.state == CircuitBreakerState.NORMAL

        # Try to start recovery from WARNING
        await risk_manager.trigger_warning("Test")
        await risk_manager.start_recovery()
        assert risk_manager.state == CircuitBreakerState.WARNING


class TestStaleFeedDetection:
    """Test stale feed detection integration."""

    @pytest.mark.asyncio
    async def test_stale_feed_triggers_warning(self, config):
        """Test that stale market feed triggers WARNING."""
        risk_manager = RiskManager(config=config)

        # Set up market with old timestamp
        state = risk_manager.get_market_state("token1")
        state.last_update_time = datetime.utcnow() - timedelta(seconds=60)

        # Check for stale feeds
        stale = risk_manager.check_stale_feeds()

        assert "token1" in stale
        assert risk_manager.is_feed_stale("token1")

        # Wait for async task to complete
        await asyncio.sleep(0.05)

        # Should trigger WARNING
        assert risk_manager.state == CircuitBreakerState.WARNING

    @pytest.mark.asyncio
    async def test_fresh_feed_clears_stale_status(self, config):
        """Test that receiving feed update clears stale status."""
        risk_manager = RiskManager(config=config)

        # Set up stale market
        state = risk_manager.get_market_state("token1")
        state.last_update_time = datetime.utcnow() - timedelta(seconds=60)
        risk_manager.check_stale_feeds()

        assert risk_manager.is_feed_stale("token1")

        # Receive fresh feed
        risk_manager.update_feed_timestamp("token1")

        # No longer stale
        assert not risk_manager.is_feed_stale("token1")


class TestInventoryRiskIntegration:
    """Test integration between InventoryManager and RiskManager."""

    @pytest.mark.asyncio
    async def test_inventory_limits_respect_circuit_breaker(self, config, inventory_manager):
        """Test that inventory limits are adjusted by circuit breaker state."""
        risk_manager = RiskManager(config=config)

        # Set up position
        inventory_manager.set_position("token1", size=50, avg_entry_price=0.5)

        # In NORMAL state, full capacity
        base_limit = config.max_position_per_market
        adjusted_limit = risk_manager.get_adjusted_position_limit(base_limit)
        assert adjusted_limit == base_limit

        # Remaining capacity calculation
        position = inventory_manager.get_position("token1")
        remaining = adjusted_limit - position.size
        assert remaining == 50

        # Trigger WARNING
        await risk_manager.trigger_warning("Test")

        # Now adjusted limit is 50%
        adjusted_limit = risk_manager.get_adjusted_position_limit(base_limit)
        assert adjusted_limit == base_limit // 2  # 50

        # With position of 50, remaining capacity is 0
        remaining = max(0, adjusted_limit - position.size)
        assert remaining == 0  # At capacity for WARNING state

    @pytest.mark.asyncio
    async def test_pnl_tracking_from_fills(self, config, inventory_manager):
        """Test P&L tracking updates from fills."""
        risk_manager = RiskManager(config=config)

        # Simulate a fill
        fill = Fill(
            order_id="order1",
            token_id="token1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=0.0,
        )
        inventory_manager.update_from_fill(fill)

        # Get position and update risk manager
        position = inventory_manager.get_position("token1")
        current_price = 0.55  # 5 cent profit per share

        risk_manager.update_from_position("token1", position, current_price)

        # Check P&L
        market_state = risk_manager.get_market_state("token1")
        expected_unrealized = (0.55 - 0.50) * 10.0  # 0.5 USDC
        assert market_state.unrealized_pnl == pytest.approx(expected_unrealized, rel=1e-9)


class TestMultiMarketRiskManagement:
    """Test risk management across multiple markets."""

    @pytest.mark.asyncio
    async def test_one_market_halt_doesnt_affect_others(self, config):
        """Test that per-market halt only affects that market."""
        risk_manager = RiskManager(config=config)

        # Set up two markets
        risk_manager.update_feed_timestamp("token1")
        risk_manager.update_feed_timestamp("token2")

        # Create drawdown on token1 only
        risk_manager.update_market_pnl("token1", realized_pnl=30.0, unrealized_pnl=0.0)
        risk_manager.update_market_pnl("token1", realized_pnl=5.0, unrealized_pnl=0.0)

        await asyncio.sleep(0.05)

        # token1 should be halted
        allowed1, reason1 = risk_manager.can_place_orders_for_market("token1")
        assert allowed1 is False
        assert "halted" in reason1.lower()

        # token2 should still be allowed
        allowed2, reason2 = risk_manager.can_place_orders_for_market("token2")
        assert allowed2 is True
        assert reason2 == ""

        # Global state should still be NORMAL
        assert risk_manager.state == CircuitBreakerState.NORMAL

    @pytest.mark.asyncio
    async def test_global_halt_affects_all_markets(self, config):
        """Test that global halt affects all markets."""
        risk_manager = RiskManager(config=config)

        # Set up markets
        risk_manager.update_feed_timestamp("token1")
        risk_manager.update_feed_timestamp("token2")
        risk_manager.update_feed_timestamp("token3")

        # Trigger global halt
        await risk_manager.trigger_halt("Manual global halt")

        # All markets should be blocked
        for token in ["token1", "token2", "token3"]:
            allowed, reason = risk_manager.can_place_orders_for_market(token)
            assert allowed is False
            assert "circuit breaker" in reason.lower()

        # Global can_place_orders should be False
        assert risk_manager.can_place_orders() is False
