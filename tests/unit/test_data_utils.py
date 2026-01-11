"""
Unit tests for poly_data/data_utils.py

Tests:
- get_position(): Position retrieval
- set_position(): Position updates with average price calculation
- get_order(): Order retrieval
- set_order(): Order updates
"""

import pytest
from unittest.mock import patch, MagicMock
import time

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestGetPosition:
    """Tests for get_position()."""

    def test_returns_existing_position(self):
        """Should return position if it exists."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token123": {"size": 100.0, "avgPrice": 0.50}}

            from poly_data.data_utils import get_position

            result = get_position("token123")

            assert result["size"] == 100.0
            assert result["avgPrice"] == 0.50

    def test_returns_zero_for_missing_position(self):
        """Should return zero position if token not found."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {}

            from poly_data.data_utils import get_position

            result = get_position("nonexistent_token")

            assert result["size"] == 0
            assert result["avgPrice"] == 0

    def test_converts_token_to_string(self):
        """Should handle numeric token IDs."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"12345": {"size": 50.0, "avgPrice": 0.45}}

            from poly_data.data_utils import get_position

            result = get_position(12345)  # Pass as int

            assert result["size"] == 50.0


class TestSetPosition:
    """Tests for set_position()."""

    def test_creates_new_position_on_buy(self):
        """Should create new position when buying into empty."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token123", "BUY", size=100.0, price=0.50)

            assert "token123" in mock_state.positions
            assert mock_state.positions["token123"]["size"] == 100.0
            assert mock_state.positions["token123"]["avgPrice"] == 0.50

    def test_updates_avg_price_on_additional_buy(self):
        """Should calculate weighted average price on additional buys."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token123": {"size": 100.0, "avgPrice": 0.40}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token123", "BUY", size=100.0, price=0.60)

            # New avg = (0.40 * 100 + 0.60 * 100) / 200 = 0.50
            assert mock_state.positions["token123"]["size"] == 200.0
            assert mock_state.positions["token123"]["avgPrice"] == 0.50

    def test_sell_reduces_size_keeps_avg_price(self):
        """Selling should reduce size but keep average price."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token123": {"size": 100.0, "avgPrice": 0.50}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token123", "SELL", size=50.0, price=0.60)

            assert mock_state.positions["token123"]["size"] == 50.0  # 100 - 50
            assert mock_state.positions["token123"]["avgPrice"] == 0.50  # Unchanged

    def test_updates_last_trade_timestamp(self):
        """Should update last_trade_update timestamp."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            before = time.time()
            set_position("token123", "BUY", size=100.0, price=0.50)
            after = time.time()

            assert "token123" in mock_state.last_trade_update
            assert before <= mock_state.last_trade_update["token123"] <= after

    def test_handles_case_insensitive_side(self):
        """Should handle both 'buy'/'BUY' and 'sell'/'SELL'."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token123": {"size": 100.0, "avgPrice": 0.50}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            # Lowercase sell
            set_position("token123", "sell", size=25.0, price=0.55)
            assert mock_state.positions["token123"]["size"] == 75.0

    def test_full_position_exit(self):
        """Should handle selling entire position."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token123": {"size": 100.0, "avgPrice": 0.50}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token123", "SELL", size=100.0, price=0.60)

            assert mock_state.positions["token123"]["size"] == 0.0


class TestGetOrder:
    """Tests for get_order()."""

    def test_returns_existing_order(self):
        """Should return order if it exists."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {
                "token123": {
                    "buy": {"price": 0.45, "size": 100.0},
                    "sell": {"price": 0.55, "size": 100.0},
                }
            }

            from poly_data.data_utils import get_order

            result = get_order("token123")

            assert result["buy"]["price"] == 0.45
            assert result["sell"]["price"] == 0.55

    def test_returns_empty_order_for_missing_token(self):
        """Should return empty order structure for unknown token."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {}

            from poly_data.data_utils import get_order

            result = get_order("nonexistent")

            assert result["buy"]["price"] == 0
            assert result["buy"]["size"] == 0
            assert result["sell"]["price"] == 0
            assert result["sell"]["size"] == 0

    def test_fills_missing_buy_side(self):
        """Should fill in missing buy side with zeros."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {"token123": {"sell": {"price": 0.55, "size": 100.0}}}

            from poly_data.data_utils import get_order

            result = get_order("token123")

            assert result["buy"]["price"] == 0
            assert result["buy"]["size"] == 0
            assert result["sell"]["price"] == 0.55

    def test_fills_missing_sell_side(self):
        """Should fill in missing sell side with zeros."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {"token123": {"buy": {"price": 0.45, "size": 100.0}}}

            from poly_data.data_utils import get_order

            result = get_order("token123")

            assert result["buy"]["price"] == 0.45
            assert result["sell"]["price"] == 0
            assert result["sell"]["size"] == 0


class TestSetOrder:
    """Tests for set_order()."""

    def test_sets_buy_order(self):
        """Should set buy order correctly."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {}

            from poly_data.data_utils import set_order

            set_order("token123", "buy", size=100.0, price=0.45)

            assert mock_state.orders["token123"]["buy"]["price"] == 0.45
            assert mock_state.orders["token123"]["buy"]["size"] == 100.0

    def test_sets_sell_order(self):
        """Should set sell order correctly."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {}

            from poly_data.data_utils import set_order

            set_order("token123", "sell", size=50.0, price=0.55)

            assert mock_state.orders["token123"]["sell"]["price"] == 0.55
            assert mock_state.orders["token123"]["sell"]["size"] == 50.0

    def test_converts_types(self):
        """Should convert price and size to floats."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.orders = {}

            from poly_data.data_utils import set_order

            set_order("token123", "buy", size="100", price="0.45")

            assert isinstance(mock_state.orders["token123"]["buy"]["price"], float)
            assert isinstance(mock_state.orders["token123"]["buy"]["size"], float)


class TestPositionAveragePriceCalculations:
    """Detailed tests for average price calculation edge cases."""

    def test_buying_at_same_price(self):
        """Buying more at same price should keep avg price."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token": {"size": 100.0, "avgPrice": 0.50}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token", "BUY", 100.0, 0.50)

            assert mock_state.positions["token"]["avgPrice"] == 0.50

    def test_buying_at_higher_price_increases_avg(self):
        """Buying at higher price should increase average."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token": {"size": 100.0, "avgPrice": 0.40}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token", "BUY", 100.0, 0.60)

            # (0.40 * 100 + 0.60 * 100) / 200 = 0.50
            assert mock_state.positions["token"]["avgPrice"] == 0.50

    def test_buying_at_lower_price_decreases_avg(self):
        """Buying at lower price should decrease average."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token": {"size": 100.0, "avgPrice": 0.60}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token", "BUY", 100.0, 0.40)

            # (0.60 * 100 + 0.40 * 100) / 200 = 0.50
            assert mock_state.positions["token"]["avgPrice"] == 0.50

    def test_sell_partial_keeps_avg(self):
        """Partial sell should keep average price unchanged."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token": {"size": 100.0, "avgPrice": 0.50}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token", "SELL", 30.0, 0.70)  # Sell at profit

            assert mock_state.positions["token"]["size"] == 70.0
            assert mock_state.positions["token"]["avgPrice"] == 0.50  # Unchanged

    def test_uneven_position_sizes(self):
        """Test with uneven position sizes."""
        with patch("poly_data.data_utils.global_state") as mock_state:
            mock_state.positions = {"token": {"size": 200.0, "avgPrice": 0.40}}
            mock_state.last_trade_update = {}

            from poly_data.data_utils import set_position

            set_position("token", "BUY", 100.0, 0.70)

            # (0.40 * 200 + 0.70 * 100) / 300 = (80 + 70) / 300 = 0.50
            assert mock_state.positions["token"]["size"] == 300.0
            assert mock_state.positions["token"]["avgPrice"] == 0.50
