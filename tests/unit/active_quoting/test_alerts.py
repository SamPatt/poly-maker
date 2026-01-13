"""
Unit tests for Active Quoting Alerts module.

Tests:
- Fill alert throttling
- Alert message formatting
- Batch fill aggregation
- All alert types
"""
import time
import pytest
from unittest.mock import patch, MagicMock

from rebates.active_quoting.alerts import (
    FillAlertThrottler,
    send_active_quoting_startup_alert,
    send_active_quoting_shutdown_alert,
    send_active_quoting_fill_alert,
    send_active_quoting_circuit_breaker_alert,
    send_active_quoting_daily_summary,
    send_active_quoting_error_alert,
    send_active_quoting_market_halt_alert,
    FILL_ALERT_THROTTLE_SECONDS,
)


# --- FillAlertThrottler Tests ---

class TestFillAlertThrottler:
    """Tests for FillAlertThrottler class."""

    def test_initial_state(self):
        """Test throttler starts with empty state."""
        throttler = FillAlertThrottler()
        assert len(throttler.last_alert_time) == 0
        assert len(throttler.pending_fills) == 0
        assert throttler.fill_count_since_summary == 0

    def test_should_send_first_fill(self):
        """Test first fill should always send alert."""
        throttler = FillAlertThrottler()
        assert throttler.should_send_fill_alert("market_1") is True

    def test_should_not_send_rapid_fills(self):
        """Test rapid fills are throttled."""
        throttler = FillAlertThrottler()
        throttler.last_alert_time["market_1"] = time.time()
        assert throttler.should_send_fill_alert("market_1") is False

    def test_should_send_after_throttle_period(self):
        """Test fills allowed after throttle period."""
        throttler = FillAlertThrottler()
        throttler.last_alert_time["market_1"] = time.time() - (FILL_ALERT_THROTTLE_SECONDS + 1)
        assert throttler.should_send_fill_alert("market_1") is True

    def test_record_fill_queues_fill(self):
        """Test recording a fill adds to pending queue."""
        throttler = FillAlertThrottler()
        throttler.last_alert_time["market_1"] = time.time()  # Recent alert

        result = throttler.record_fill(
            market_name="market_1",
            side="BUY",
            price=0.50,
            size=10.0,
        )

        assert result is None  # Not ready to send
        assert len(throttler.pending_fills["market_1"]) == 1
        assert throttler.fill_count_since_summary == 1

    def test_record_fill_returns_batch_when_ready(self):
        """Test recording fill returns batch when throttle period passed."""
        throttler = FillAlertThrottler()
        throttler.pending_fills["market_1"].append({
            "side": "BUY",
            "price": 0.49,
            "size": 5.0,
        })
        # No last_alert_time means first alert is ready

        result = throttler.record_fill(
            market_name="market_1",
            side="SELL",
            price=0.51,
            size=5.0,
        )

        assert result is not None
        assert result["market_name"] == "market_1"
        assert len(result["fills"]) == 2
        assert len(throttler.pending_fills["market_1"]) == 0  # Cleared

    def test_record_fill_with_markout(self):
        """Test recording fill with markout data."""
        throttler = FillAlertThrottler()
        result = throttler.record_fill(
            market_name="market_1",
            side="BUY",
            price=0.50,
            size=10.0,
            markout_bps=50.0,
        )

        assert result is not None
        assert result["fills"][0]["markout_bps"] == 50.0

    def test_flush_all_returns_all_pending(self):
        """Test flush_all returns all pending fills."""
        throttler = FillAlertThrottler()
        throttler.last_alert_time["market_1"] = time.time()
        throttler.last_alert_time["market_2"] = time.time()

        throttler.record_fill("market_1", "BUY", 0.50, 10.0)
        throttler.record_fill("market_2", "SELL", 0.60, 20.0)

        batches = throttler.flush_all()

        assert len(batches) == 2
        market_names = [b["market_name"] for b in batches]
        assert "market_1" in market_names
        assert "market_2" in market_names
        assert len(throttler.pending_fills["market_1"]) == 0
        assert len(throttler.pending_fills["market_2"]) == 0

    def test_different_markets_independent(self):
        """Test throttling is per-market."""
        throttler = FillAlertThrottler()
        throttler.last_alert_time["market_1"] = time.time()  # Recently alerted

        # Market 1 should be throttled
        assert throttler.should_send_fill_alert("market_1") is False

        # Market 2 should not be throttled (no prior alert)
        assert throttler.should_send_fill_alert("market_2") is True


# --- Alert Function Tests ---

class TestStartupAlert:
    """Tests for startup alert."""

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_startup_alert_basic(self, mock_send):
        """Test basic startup alert."""
        mock_send.return_value = True

        result = send_active_quoting_startup_alert(
            market_count=5,
            dry_run=False,
        )

        assert result is True
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        message = call_args[0][0]
        assert "Active Quoting Bot Started" in message
        assert "Markets:</b> 5" in message
        assert "LIVE" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_startup_alert_dry_run(self, mock_send):
        """Test startup alert in dry run mode."""
        mock_send.return_value = True

        send_active_quoting_startup_alert(
            market_count=3,
            dry_run=True,
        )

        message = mock_send.call_args[0][0]
        assert "DRY RUN" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_startup_alert_with_config(self, mock_send):
        """Test startup alert with config summary."""
        mock_send.return_value = True

        send_active_quoting_startup_alert(
            market_count=5,
            dry_run=False,
            config_summary={
                "order_size": 25.0,
                "max_position": 100,
            },
        )

        message = mock_send.call_args[0][0]
        assert "$25.00" in message
        assert "100" in message


class TestShutdownAlert:
    """Tests for shutdown alert."""

    @patch("rebates.active_quoting.alerts._fill_throttler")
    @patch("rebates.active_quoting.alerts.send_alert")
    def test_shutdown_alert_basic(self, mock_send, mock_throttler):
        """Test basic shutdown alert."""
        mock_send.return_value = True
        mock_throttler.flush_all.return_value = []

        result = send_active_quoting_shutdown_alert(
            reason="Normal shutdown",
        )

        assert result is True
        mock_send.assert_called()
        message = mock_send.call_args[0][0]
        assert "Active Quoting Bot Stopped" in message
        assert "Normal shutdown" in message

    @patch("rebates.active_quoting.alerts._fill_throttler")
    @patch("rebates.active_quoting.alerts.send_alert")
    def test_shutdown_alert_with_stats(self, mock_send, mock_throttler):
        """Test shutdown alert with statistics."""
        mock_send.return_value = True
        mock_throttler.flush_all.return_value = []

        send_active_quoting_shutdown_alert(
            reason="Error occurred",
            stats={
                "total_fills": 42,
                "net_fees": 5.25,
                "realized_pnl": -2.50,
            },
        )

        message = mock_send.call_args[0][0]
        assert "42" in message
        assert "$5.25" in message
        assert "$-2.50" in message


class TestFillAlert:
    """Tests for fill alert."""

    @patch("rebates.active_quoting.alerts._fill_throttler")
    @patch("rebates.active_quoting.alerts._send_single_fill_alert")
    def test_fill_alert_force_bypasses_throttle(self, mock_single, mock_throttler):
        """Test force parameter bypasses throttling."""
        mock_single.return_value = True

        send_active_quoting_fill_alert(
            market_name="BTC Up",
            side="BUY",
            price=0.50,
            size=10.0,
            force=True,
        )

        mock_single.assert_called_once()
        mock_throttler.record_fill.assert_not_called()

    @patch("rebates.active_quoting.alerts._fill_throttler")
    def test_fill_alert_queues_when_throttled(self, mock_throttler):
        """Test fill is queued when throttled."""
        mock_throttler.record_fill.return_value = None

        result = send_active_quoting_fill_alert(
            market_name="BTC Up",
            side="BUY",
            price=0.50,
            size=10.0,
        )

        assert result is True  # Queued
        mock_throttler.record_fill.assert_called_once()

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_single_fill_alert_formatting(self, mock_send):
        """Test single fill alert message format."""
        mock_send.return_value = True

        from rebates.active_quoting.alerts import _send_single_fill_alert

        _send_single_fill_alert(
            market_name="ETH Down",
            side="SELL",
            price=0.45,
            size=25.0,
            markout_bps=-50.0,
        )

        message = mock_send.call_args[0][0]
        assert "Active Quote Fill" in message
        assert "ETH Down" in message
        assert "SELL" in message
        assert "0.4500" in message
        assert "25.00" in message
        assert "-50.0 bps" in message


class TestCircuitBreakerAlert:
    """Tests for circuit breaker alert."""

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_circuit_breaker_halt(self, mock_send):
        """Test circuit breaker halt alert."""
        mock_send.return_value = True

        send_active_quoting_circuit_breaker_alert(
            old_state="NORMAL",
            new_state="HALTED",
            reason="Max drawdown exceeded",
            details={"drawdown": 50.0},
        )

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        message = call_args[0][0]
        assert "HALTED" in message
        assert "NORMAL" in message
        assert "Max drawdown exceeded" in message
        assert "$50.00" in message
        # HALTED should wait for delivery
        assert call_args[1].get("wait") is True

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_circuit_breaker_warning(self, mock_send):
        """Test circuit breaker warning alert."""
        mock_send.return_value = True

        send_active_quoting_circuit_breaker_alert(
            old_state="NORMAL",
            new_state="WARNING",
            reason="Stale feed detected",
        )

        message = mock_send.call_args[0][0]
        assert "WARNING" in message


class TestDailySummaryAlert:
    """Tests for daily summary alert."""

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_daily_summary_basic(self, mock_send):
        """Test basic daily summary alert."""
        mock_send.return_value = True

        send_active_quoting_daily_summary(
            session_duration_hours=12.5,
            total_fills=100,
            total_volume=500.0,
            total_notional=250.0,
            net_fees=2.50,
            realized_pnl=5.00,
            market_count=8,
        )

        message = mock_send.call_args[0][0]
        assert "Active Quoting Summary" in message
        assert "12.5 hours" in message
        assert "100" in message
        assert "500.00 shares" in message
        assert "$250.00" in message
        assert "$2.50" in message
        assert "$5.00" in message
        assert "8" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_daily_summary_with_markouts(self, mock_send):
        """Test daily summary with markout stats."""
        mock_send.return_value = True

        send_active_quoting_daily_summary(
            session_duration_hours=24.0,
            total_fills=200,
            total_volume=1000.0,
            total_notional=500.0,
            net_fees=10.0,
            realized_pnl=15.0,
            markout_stats={
                5: 25.0,
                15: -10.0,
            },
            market_count=16,
        )

        message = mock_send.call_args[0][0]
        assert "Markouts" in message
        assert "5s: 25.0 bps" in message
        assert "15s: -10.0 bps" in message


class TestErrorAlert:
    """Tests for error alert."""

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_error_alert_basic(self, mock_send):
        """Test basic error alert."""
        mock_send.return_value = True

        send_active_quoting_error_alert(
            error_type="order_placement",
            message_text="Connection timeout",
        )

        message = mock_send.call_args[0][0]
        assert "Active Quoting Error" in message
        assert "order_placement" in message
        assert "Connection timeout" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_error_alert_with_market(self, mock_send):
        """Test error alert with market context."""
        mock_send.return_value = True

        send_active_quoting_error_alert(
            error_type="websocket",
            message_text="Disconnected unexpectedly",
            market_name="BTC Up",
        )

        message = mock_send.call_args[0][0]
        assert "BTC Up" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_error_alert_truncates_long_message(self, mock_send):
        """Test error alert truncates long messages."""
        mock_send.return_value = True

        long_message = "x" * 300

        send_active_quoting_error_alert(
            error_type="unknown",
            message_text=long_message,
        )

        message = mock_send.call_args[0][0]
        assert "..." in message
        assert len(message) < 500


class TestMarketHaltAlert:
    """Tests for market halt alert."""

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_market_halt_alert(self, mock_send):
        """Test market halt alert."""
        mock_send.return_value = True

        send_active_quoting_market_halt_alert(
            market_name="SOL Down",
            reason="Drawdown limit exceeded",
        )

        message = mock_send.call_args[0][0]
        assert "Market Halted" in message
        assert "SOL Down" in message
        assert "Drawdown limit exceeded" in message

    @patch("rebates.active_quoting.alerts.send_alert")
    def test_market_halt_truncates_long_name(self, mock_send):
        """Test market halt truncates long market names."""
        mock_send.return_value = True

        long_name = "A" * 50

        send_active_quoting_market_halt_alert(
            market_name=long_name,
            reason="Test",
        )

        message = mock_send.call_args[0][0]
        assert "..." in message
