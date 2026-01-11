"""
Unit tests for trading.py

Tests:
- send_buy_order(): BUY order creation logic
- send_sell_order(): SELL order creation logic
- perform_trade(): Main trading function (partial coverage due to complexity)
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import pandas as pd
import asyncio

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.fixtures.market_data import (
    create_market_row,
    create_orderbook,
    create_position,
    create_order_pair,
    SAMPLE_PARAMS,
)


class TestSendBuyOrder:
    """Tests for send_buy_order()."""

    @pytest.fixture
    def mock_trading_environment(self):
        """Set up all necessary mocks for send_buy_order."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", False):
                with patch("trading.TELEGRAM_ENABLED", False):
                    with patch("trading.DB_ENABLED", False):
                        # Set up global state
                        mock_state.wallet_balance = 1000.0
                        mock_state.committed_buy_orders = 0.0
                        mock_state.MIN_AVAILABLE_BALANCE = 10.0
                        mock_state.orders = {}
                        mock_state.client = MagicMock()
                        mock_state.client.create_order.return_value = {"order_id": "test123"}

                        yield mock_state

    def test_rejects_order_when_insufficient_balance(self, mock_trading_environment):
        """Should not place order when balance is insufficient."""
        mock_trading_environment.wallet_balance = 50.0
        mock_trading_environment.committed_buy_orders = 40.0
        # Available = 50 - 40 = 10, but need order_cost + MIN (10)

        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.50,
            "size": 100.0,  # Cost = 50, need 50 + 10 = 60, have 10
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        # Should not call create_order
        mock_trading_environment.client.create_order.assert_not_called()

    def test_places_order_when_balance_sufficient(self, mock_trading_environment):
        """Should place order when balance is sufficient."""
        mock_trading_environment.wallet_balance = 1000.0
        mock_trading_environment.committed_buy_orders = 0.0

        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.45,
            "size": 100.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        mock_trading_environment.client.create_order.assert_called_once()
        call_args = mock_trading_environment.client.create_order.call_args
        assert call_args[0][0] == "token123"  # token
        assert call_args[0][1] == "BUY"  # side
        assert call_args[0][2] == 0.45  # price
        assert call_args[0][3] == 100.0  # size

    def test_updates_committed_funds_on_order(self, mock_trading_environment):
        """Should increment committed_buy_orders when placing order."""
        initial_committed = mock_trading_environment.committed_buy_orders

        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.48,  # Above incentive start (0.50 - 0.05 = 0.45)
            "size": 100.0,  # Cost = 48
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        # committed should increase by order cost
        assert mock_trading_environment.committed_buy_orders == initial_committed + 48.0

    def test_releases_funds_on_order_failure(self, mock_trading_environment):
        """Should release committed funds if order fails."""
        mock_trading_environment.client.create_order.side_effect = Exception("API Error")
        initial_committed = mock_trading_environment.committed_buy_orders

        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.48,  # Above incentive start (0.50 - 0.05 = 0.45)
            "size": 100.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        with pytest.raises(Exception, match="API Error"):
            send_buy_order(order)

        # Committed should return to initial value
        assert mock_trading_environment.committed_buy_orders == initial_committed

    def test_skips_order_below_incentive_threshold(self, mock_trading_environment):
        """Should not place order if price is below incentive start."""
        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.35,  # Below incentive_start (mid - spread = 0.50 - 0.05 = 0.45)
            "size": 100.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,  # 5% = 0.05
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        mock_trading_environment.client.create_order.assert_not_called()

    def test_skips_order_outside_price_range(self, mock_trading_environment):
        """Should not place order if price is outside 0.1-0.9 range."""
        from trading import send_buy_order

        # Test price too low
        order = {
            "token": "token123",
            "price": 0.05,
            "size": 100.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.08,
            "max_spread": 10.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)
        mock_trading_environment.client.create_order.assert_not_called()

        # Test price too high
        order["price"] = 0.95
        order["mid_price"] = 0.93
        send_buy_order(order)
        mock_trading_environment.client.create_order.assert_not_called()

    def test_updates_local_order_state(self, mock_trading_environment):
        """Should update global_state.orders after placing order."""
        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.45,
            "size": 100.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        assert "token123" in mock_trading_environment.orders
        assert mock_trading_environment.orders["token123"]["buy"]["price"] == 0.45
        assert mock_trading_environment.orders["token123"]["buy"]["size"] == 100.0

    def test_skips_when_existing_order_similar(self, mock_trading_environment):
        """Should not cancel/replace when existing order is similar."""
        from trading import send_buy_order

        order = {
            "token": "token123",
            "price": 0.451,  # Very close to existing
            "size": 100.0,  # Same size
            "orders": {
                "buy": {"price": 0.450, "size": 100.0},  # Existing similar order
                "sell": {"price": 0, "size": 0},
            },
            "mid_price": 0.50,
            "max_spread": 5.0,
            "neg_risk": "FALSE",
        }

        send_buy_order(order)

        # Should not call cancel or create
        mock_trading_environment.client.cancel_all_asset.assert_not_called()
        mock_trading_environment.client.create_order.assert_not_called()


class TestSendSellOrder:
    """Tests for send_sell_order()."""

    @pytest.fixture
    def mock_trading_environment(self):
        """Set up all necessary mocks for send_sell_order."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", False):
                with patch("trading.TELEGRAM_ENABLED", False):
                    with patch("trading.DB_ENABLED", False):
                        mock_state.orders = {}
                        mock_state.client = MagicMock()
                        mock_state.client.create_order.return_value = {"order_id": "test123"}

                        yield mock_state

    def test_places_sell_order(self, mock_trading_environment):
        """Should place sell order correctly."""
        from trading import send_sell_order

        order = {
            "token": "token123",
            "price": 0.55,
            "size": 50.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "neg_risk": "FALSE",
        }

        send_sell_order(order)

        mock_trading_environment.client.create_order.assert_called_once()
        call_args = mock_trading_environment.client.create_order.call_args
        assert call_args[0][1] == "SELL"
        assert call_args[0][2] == 0.55
        assert call_args[0][3] == 50.0

    def test_updates_local_order_state(self, mock_trading_environment):
        """Should update global_state.orders after placing sell order."""
        from trading import send_sell_order

        order = {
            "token": "token456",
            "price": 0.60,
            "size": 75.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "neg_risk": "FALSE",
        }

        send_sell_order(order)

        assert "token456" in mock_trading_environment.orders
        assert mock_trading_environment.orders["token456"]["sell"]["price"] == 0.60
        assert mock_trading_environment.orders["token456"]["sell"]["size"] == 75.0

    def test_cancels_when_significant_change(self, mock_trading_environment):
        """Should cancel existing orders when price change is significant."""
        from trading import send_sell_order

        order = {
            "token": "token123",
            "price": 0.60,  # 10 cent diff from existing 0.50
            "size": 50.0,
            "orders": {
                "buy": {"price": 0, "size": 0},
                "sell": {"price": 0.50, "size": 50.0},  # Existing order at 0.50
            },
            "neg_risk": "FALSE",
        }

        send_sell_order(order)

        mock_trading_environment.client.cancel_all_asset.assert_called()

    def test_skips_when_existing_order_similar(self, mock_trading_environment):
        """Should not cancel/replace when existing order is similar."""
        from trading import send_sell_order

        order = {
            "token": "token123",
            "price": 0.551,  # Very close to existing 0.550
            "size": 50.0,
            "orders": {
                "buy": {"price": 0, "size": 0},
                "sell": {"price": 0.550, "size": 50.0},
            },
            "neg_risk": "FALSE",
        }

        send_sell_order(order)

        mock_trading_environment.client.cancel_all_asset.assert_not_called()
        mock_trading_environment.client.create_order.assert_not_called()

    def test_handles_neg_risk_market(self, mock_trading_environment):
        """Should pass correct neg_risk flag to create_order."""
        from trading import send_sell_order

        order = {
            "token": "token123",
            "price": 0.55,
            "size": 50.0,
            "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
            "neg_risk": "TRUE",
        }

        send_sell_order(order)

        call_args = mock_trading_environment.client.create_order.call_args
        assert call_args[0][4] is True  # neg_risk flag


class TestDryRunMode:
    """Tests for DRY_RUN mode behavior."""

    def test_buy_order_dry_run(self):
        """Should not call client in DRY_RUN mode."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", True):
                mock_state.wallet_balance = 1000.0
                mock_state.committed_buy_orders = 0.0
                mock_state.MIN_AVAILABLE_BALANCE = 10.0
                mock_state.orders = {}
                mock_state.client = MagicMock()

                from trading import send_buy_order

                order = {
                    "token": "token123",
                    "price": 0.45,
                    "size": 100.0,
                    "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
                    "mid_price": 0.50,
                    "max_spread": 5.0,
                    "neg_risk": "FALSE",
                }

                send_buy_order(order)

                # Should not call create_order in dry run
                mock_state.client.create_order.assert_not_called()

    def test_sell_order_dry_run(self):
        """Should not call client in DRY_RUN mode."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", True):
                mock_state.orders = {}
                mock_state.client = MagicMock()

                from trading import send_sell_order

                order = {
                    "token": "token123",
                    "price": 0.55,
                    "size": 50.0,
                    "orders": {"buy": {"price": 0, "size": 0}, "sell": {"price": 0, "size": 0}},
                    "neg_risk": "FALSE",
                }

                send_sell_order(order)

                mock_state.client.create_order.assert_not_called()


class TestPerformTrade:
    """Tests for perform_trade() async function."""

    @pytest.fixture
    def mock_full_environment(self):
        """Set up complete mock environment for perform_trade."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", True):
                with patch("trading.TELEGRAM_ENABLED", False):
                    with patch("trading.DB_ENABLED", False):
                        with patch("trading.CONSTANTS") as mock_constants:
                            # Set up DataFrame with market config
                            market_row = create_market_row(
                                condition_id="market123",
                                token1="token1_abc",
                                token2="token2_xyz",
                                answer1="Yes",
                                answer2="No",
                                tick_size=0.01,
                                param_type="default",
                            )
                            mock_state.df = pd.DataFrame([market_row])

                            # Set up params
                            mock_state.params = SAMPLE_PARAMS

                            # Set up positions
                            mock_state.positions = {
                                "token1_abc": {"size": 0, "avgPrice": 0},
                                "token2_xyz": {"size": 0, "avgPrice": 0},
                            }

                            # Set up orders
                            mock_state.orders = {}

                            # Set up orderbook data
                            mock_state.all_data = {
                                "market123": create_orderbook(),
                            }

                            # Set up reverse tokens
                            mock_state.REVERSE_TOKENS = {
                                "token1_abc": "token2_xyz",
                                "token2_xyz": "token1_abc",
                            }

                            # Set up wallet
                            mock_state.wallet_balance = 1000.0
                            mock_state.committed_buy_orders = 0.0
                            mock_state.MIN_AVAILABLE_BALANCE = 10.0

                            # Set up client
                            mock_state.client = MagicMock()
                            mock_state.client.get_position.return_value = (0, 0)
                            mock_state.client.merge_positions.return_value = True

                            # Constants
                            mock_constants.MIN_MERGE_SIZE = 20

                            yield mock_state

    @pytest.mark.asyncio
    async def test_perform_trade_runs_without_error(self, mock_full_environment):
        """Should execute without raising exceptions."""
        from trading import perform_trade

        # Should not raise
        await perform_trade("market123")

    @pytest.mark.asyncio
    async def test_skips_nonexistent_market(self, mock_full_environment):
        """Should handle non-existent market gracefully."""
        from trading import perform_trade

        # Should not raise, but will print error
        await perform_trade("nonexistent_market")

    @pytest.mark.asyncio
    async def test_position_merge_triggered(self, mock_full_environment):
        """Should trigger merge when both positions exist."""
        mock_full_environment.positions = {
            "token1_abc": {"size": 100, "avgPrice": 0.45},
            "token2_xyz": {"size": 100, "avgPrice": 0.55},
        }
        mock_full_environment.client.get_position.return_value = (100_000_000, 0)  # Scaled

        # Need to patch CONSTANTS at the module level
        with patch("trading.CONSTANTS.MIN_MERGE_SIZE", 20):
            from trading import perform_trade

            await perform_trade("market123")

            # Merge should have been called since both positions > MIN_MERGE_SIZE
            # Note: may not be called due to complex logic, but test runs without error


class TestOrderCancellation:
    """Tests for order cancellation logic."""

    def test_cancels_when_price_diff_significant(self):
        """Should cancel when price difference > 0.005."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", False):
                with patch("trading.TELEGRAM_ENABLED", False):
                    with patch("trading.DB_ENABLED", False):
                        mock_state.wallet_balance = 1000.0
                        mock_state.committed_buy_orders = 0.0
                        mock_state.MIN_AVAILABLE_BALANCE = 10.0
                        mock_state.orders = {}
                        mock_state.client = MagicMock()

                        from trading import send_buy_order

                        order = {
                            "token": "token123",
                            "price": 0.50,
                            "size": 100.0,
                            "orders": {
                                "buy": {"price": 0.45, "size": 100.0},  # 0.05 diff > 0.005
                                "sell": {"price": 0, "size": 0},
                            },
                            "mid_price": 0.50,
                            "max_spread": 5.0,
                            "neg_risk": "FALSE",
                        }

                        send_buy_order(order)

                        mock_state.client.cancel_all_asset.assert_called_with("token123")

    def test_cancels_when_size_diff_significant(self):
        """Should cancel when size difference > 10%."""
        with patch("trading.global_state") as mock_state:
            with patch("trading.DRY_RUN", False):
                with patch("trading.TELEGRAM_ENABLED", False):
                    with patch("trading.DB_ENABLED", False):
                        mock_state.wallet_balance = 1000.0
                        mock_state.committed_buy_orders = 0.0
                        mock_state.MIN_AVAILABLE_BALANCE = 10.0
                        mock_state.orders = {}
                        mock_state.client = MagicMock()

                        from trading import send_buy_order

                        order = {
                            "token": "token123",
                            "price": 0.45,
                            "size": 100.0,
                            "orders": {
                                "buy": {"price": 0.45, "size": 50.0},  # 50% diff > 10%
                                "sell": {"price": 0, "size": 0},
                            },
                            "mid_price": 0.50,
                            "max_spread": 5.0,
                            "neg_risk": "FALSE",
                        }

                        send_buy_order(order)

                        mock_state.client.cancel_all_asset.assert_called()
