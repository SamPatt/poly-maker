"""
Shared pytest fixtures for Poly-Maker tests.

Provides:
- Mock global_state with controlled data
- Mock Polymarket client
- Sample market/order/position data
- Helper utilities for testing
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from sortedcontainers import SortedDict
import pandas as pd
import sys
import os

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures.market_data import (
    create_orderbook,
    create_empty_orderbook,
    create_thin_orderbook,
    create_market_row,
    create_position,
    create_order_pair,
    SAMPLE_PARAMS,
    SAMPLE_TOKENS,
)


@pytest.fixture
def sample_orderbook():
    """Provide a sample orderbook with realistic bid/ask data."""
    return create_orderbook()


@pytest.fixture
def empty_orderbook():
    """Provide an empty orderbook."""
    return create_empty_orderbook()


@pytest.fixture
def thin_orderbook():
    """Provide a thin orderbook with minimal liquidity."""
    return create_thin_orderbook()


@pytest.fixture
def sample_market_row():
    """Provide a sample market configuration row."""
    return create_market_row()


@pytest.fixture
def neg_risk_market_row():
    """Provide a negative risk market configuration row."""
    return create_market_row(neg_risk="TRUE")


@pytest.fixture
def sample_position():
    """Provide a sample position."""
    return create_position(size=100.0, avg_price=0.50)


@pytest.fixture
def sample_orders():
    """Provide sample buy/sell orders."""
    return create_order_pair(buy_price=0.45, buy_size=100.0, sell_price=0.55, sell_size=100.0)


@pytest.fixture
def sample_params():
    """Provide sample hyperparameters."""
    return SAMPLE_PARAMS.copy()


@pytest.fixture
def sample_tokens():
    """Provide sample token IDs."""
    return SAMPLE_TOKENS.copy()


@pytest.fixture
def mock_global_state(sample_orderbook, sample_market_row, sample_params):
    """
    Provide a mocked global_state module with controlled data.

    This fixture patches the global_state module to provide predictable
    test data without affecting real state.
    """
    with patch("poly_data.global_state") as mock_state:
        # Set up basic attributes
        mock_state.all_tokens = [sample_market_row["token1"], sample_market_row["token2"]]
        mock_state.all_data = {
            sample_market_row["token1"]: sample_orderbook,
            sample_market_row["token2"]: sample_orderbook,
        }
        mock_state.positions = {}
        mock_state.orders = {}
        mock_state.params = sample_params
        mock_state.performing = {}
        mock_state.performing_timestamps = {}
        mock_state.last_trade_update = {}
        mock_state.wallet_balance = 1000.0
        mock_state.committed_buy_orders = 0.0
        mock_state.MIN_AVAILABLE_BALANCE = 10.0
        mock_state.lock = MagicMock()
        mock_state.REVERSE_TOKENS = {
            sample_market_row["token1"]: sample_market_row["token2"],
            sample_market_row["token2"]: sample_market_row["token1"],
        }

        # Create DataFrame from market row
        mock_state.df = pd.DataFrame([sample_market_row])

        yield mock_state


@pytest.fixture
def mock_client():
    """
    Provide a mocked PolymarketClient.

    The client has common methods mocked with sensible defaults.
    """
    client = MagicMock()

    # Mock position methods
    client.get_all_positions.return_value = pd.DataFrame()
    client.get_all_orders.return_value = pd.DataFrame()

    # Mock order methods
    client.create_order.return_value = {"order_id": "test_order_123"}
    client.cancel_order.return_value = True
    client.cancel_all.return_value = True
    client.cancel_all_asset.return_value = True

    # Mock balance methods
    client.get_usdc_balance.return_value = 1000.0

    # Mock merge/redeem methods
    client.merge_positions.return_value = True
    client.redeem_positions.return_value = True

    yield client


@pytest.fixture
def mock_telegram():
    """Mock Telegram alerts to prevent actual messages being sent."""
    with patch("alerts.telegram.send_alert") as mock_send:
        mock_send.return_value = True
        yield mock_send


@pytest.fixture
def mock_telegram_disabled():
    """Mock Telegram as disabled."""
    with patch("alerts.telegram.TELEGRAM_ENABLED", False):
        yield


# --- Integration Test Fixtures ---


@pytest.fixture(scope="session")
def integration_enabled():
    """Check if integration tests should run."""
    return os.getenv("POLY_TEST_INTEGRATION", "false").lower() == "true"


@pytest.fixture
def skip_if_no_integration(integration_enabled):
    """Skip test if integration tests are disabled."""
    if not integration_enabled:
        pytest.skip("Integration tests disabled (set POLY_TEST_INTEGRATION=true)")


# --- Helper Functions ---


def assert_order_valid(order: dict) -> None:
    """Assert that an order dict has valid structure."""
    assert "price" in order
    assert "size" in order
    assert isinstance(order["price"], (int, float))
    assert isinstance(order["size"], (int, float))
    assert order["price"] >= 0
    assert order["size"] >= 0


def assert_position_valid(position: dict) -> None:
    """Assert that a position dict has valid structure."""
    assert "size" in position
    assert "avgPrice" in position
    assert isinstance(position["size"], (int, float))
    assert isinstance(position["avgPrice"], (int, float))
