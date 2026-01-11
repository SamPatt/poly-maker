"""
Unit tests for alerts/telegram.py

Tests:
- send_alert(): Base alert function
- send_trade_alert(): Trade notification formatting
- send_error_alert(): Error notification formatting
- send_stop_loss_alert(): Stop-loss notification
- Telegram disabled behavior
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestSendAlert:
    """Tests for send_alert()."""

    def test_returns_false_when_disabled(self):
        """Should return False when Telegram is disabled."""
        with patch("alerts.telegram.TELEGRAM_ENABLED", False):
            from alerts.telegram import send_alert

            result = send_alert("Test message")

            assert result is False

    def test_calls_telegram_api_when_enabled(self):
        """Should attempt to send message when enabled."""
        with patch("alerts.telegram.TELEGRAM_ENABLED", True):
            with patch("alerts.telegram._send_telegram_message", new_callable=AsyncMock) as mock_send:
                mock_send.return_value = True

                # Need to handle the asyncio event loop
                with patch("alerts.telegram.asyncio") as mock_asyncio:
                    mock_asyncio.get_event_loop.return_value.is_running.return_value = False
                    mock_asyncio.run.return_value = True

                    from alerts.telegram import send_alert

                    result = send_alert("Test message")

                    # Should attempt to run the async function
                    assert mock_asyncio.run.called or mock_asyncio.create_task.called


class TestSendTradeAlert:
    """Tests for send_trade_alert()."""

    def test_formats_buy_alert(self):
        """Should format BUY alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_trade_alert

            send_trade_alert(
                side="BUY",
                token="token123",
                price=0.45,
                size=100.0,
                market_question="Will BTC hit $100k?",
                outcome="Yes",
            )

            # Check the message contains expected elements
            call_args = mock_send.call_args[0][0]
            assert "Trade Executed" in call_args
            assert "BUY" in call_args
            assert "0.45" in call_args or "0.4500" in call_args
            assert "100" in call_args
            assert "BTC" in call_args

    def test_formats_sell_alert(self):
        """Should format SELL alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_trade_alert

            send_trade_alert(
                side="SELL", token="token123", price=0.55, size=50.0, outcome="No"
            )

            call_args = mock_send.call_args[0][0]
            assert "SELL" in call_args

    def test_truncates_long_question(self):
        """Should truncate long market questions."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_trade_alert

            long_question = "A" * 200  # Very long question

            send_trade_alert(
                side="BUY",
                token="token123",
                price=0.45,
                size=100.0,
                market_question=long_question,
            )

            call_args = mock_send.call_args[0][0]
            # Should be truncated (80 chars + "...")
            assert len(long_question) > len(call_args)
            assert "..." in call_args

    def test_handles_missing_optional_fields(self):
        """Should handle missing market_question and outcome."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_trade_alert

            # Should not raise
            result = send_trade_alert(
                side="BUY",
                token="token123",
                price=0.45,
                size=100.0,
            )

            assert mock_send.called


class TestSendErrorAlert:
    """Tests for send_error_alert()."""

    def test_formats_error_alert(self):
        """Should format error alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_error_alert

            send_error_alert(
                error_type="WebSocket",
                error_message="Connection closed unexpectedly",
                context="Market data feed",
            )

            call_args = mock_send.call_args[0][0]
            assert "Error" in call_args
            assert "WebSocket" in call_args
            assert "Connection closed" in call_args
            assert "Market data" in call_args

    def test_truncates_long_error_message(self):
        """Should truncate very long error messages."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_error_alert

            long_error = "E" * 1000

            send_error_alert(error_type="API", error_message=long_error)

            call_args = mock_send.call_args[0][0]
            # Message should be truncated to 500 chars
            assert len(call_args) < len(long_error) + 100  # Allow for formatting

    def test_handles_missing_context(self):
        """Should work without optional context."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_error_alert

            result = send_error_alert(error_type="Order", error_message="Failed to create")

            assert mock_send.called


class TestSendStopLossAlert:
    """Tests for send_stop_loss_alert()."""

    def test_formats_stop_loss_alert(self):
        """Should format stop-loss alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_stop_loss_alert

            send_stop_loss_alert(
                market_question="Will ETH hit $5k?",
                pnl=-5.5,
                position_size=200.0,
                exit_price=0.42,
            )

            call_args = mock_send.call_args[0][0]
            assert "Stop-Loss" in call_args
            assert "ETH" in call_args
            assert "-5.5" in call_args or "-5.50" in call_args
            assert "200" in call_args
            assert "paused" in call_args.lower()

    def test_truncates_long_market_question(self):
        """Should truncate long market questions."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_stop_loss_alert

            long_question = "Q" * 200

            send_stop_loss_alert(
                market_question=long_question,
                pnl=-3.0,
                position_size=100.0,
                exit_price=0.45,
            )

            call_args = mock_send.call_args[0][0]
            # Should be truncated
            assert len(call_args) < len(long_question) + 200


class TestSendStartupShutdownAlerts:
    """Tests for startup and shutdown alerts."""

    def test_startup_alert_dry_run(self):
        """Should indicate DRY RUN mode in startup alert."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_startup_alert

            send_startup_alert(dry_run=True)

            call_args = mock_send.call_args[0][0]
            assert "DRY RUN" in call_args
            assert "Started" in call_args

    def test_startup_alert_live(self):
        """Should indicate LIVE mode in startup alert."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_startup_alert

            send_startup_alert(dry_run=False)

            call_args = mock_send.call_args[0][0]
            assert "LIVE" in call_args

    def test_shutdown_alert(self):
        """Should format shutdown alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_shutdown_alert

            send_shutdown_alert(reason="User requested shutdown")

            call_args = mock_send.call_args[0][0]
            assert "Stopped" in call_args
            assert "User requested" in call_args


class TestTelegramDisabled:
    """Tests for behavior when Telegram is disabled."""

    def test_all_alerts_return_false(self):
        """All alert functions should return False when disabled."""
        with patch("alerts.telegram.TELEGRAM_ENABLED", False):
            from alerts.telegram import (
                send_alert,
                send_trade_alert,
                send_error_alert,
                send_stop_loss_alert,
                send_startup_alert,
                send_shutdown_alert,
            )

            assert send_alert("test") is False
            # Other functions call send_alert internally
            assert send_trade_alert("BUY", "token", 0.5, 100.0) is False
            assert send_error_alert("Test", "Error") is False
            assert send_stop_loss_alert("Market", -5.0, 100.0, 0.45) is False
            assert send_startup_alert() is False
            assert send_shutdown_alert() is False


class TestPositionMergedAlert:
    """Tests for send_position_merged_alert()."""

    def test_formats_merge_alert(self):
        """Should format position merged alert correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_position_merged_alert

            send_position_merged_alert(
                market_question="Will BTC hit $100k?",
                amount_merged=500.0,
                usdc_recovered=245.50,
            )

            call_args = mock_send.call_args[0][0]
            assert "Merged" in call_args
            assert "500" in call_args
            assert "245" in call_args
            assert "USDC" in call_args


class TestDailySummaryAlert:
    """Tests for send_daily_summary()."""

    def test_formats_daily_summary(self):
        """Should format daily summary correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_daily_summary

            send_daily_summary(
                total_positions=5,
                total_value=1500.0,
                daily_pnl=25.50,
                daily_trades=12,
                earnings=8.75,
            )

            call_args = mock_send.call_args[0][0]
            assert "Summary" in call_args
            assert "1500" in call_args or "1,500" in call_args
            assert "25" in call_args
            assert "12" in call_args
            assert "8" in call_args

    def test_shows_negative_pnl(self):
        """Should show negative P&L correctly."""
        with patch("alerts.telegram.send_alert") as mock_send:
            mock_send.return_value = True

            from alerts.telegram import send_daily_summary

            send_daily_summary(
                total_positions=3,
                total_value=900.0,
                daily_pnl=-15.25,
                daily_trades=8,
                earnings=5.0,
            )

            call_args = mock_send.call_args[0][0]
            assert "-15" in call_args
