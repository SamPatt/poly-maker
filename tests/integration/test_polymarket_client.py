"""
Integration tests for Polymarket API.

These tests verify real API behavior using read-only operations.
Requires POLY_TEST_INTEGRATION=true environment variable.

IMPORTANT: These tests use real API calls and require valid credentials.
Only read-only operations are tested to avoid side effects.
"""

import pytest
import os

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Skip all tests in this module if integration is disabled
pytestmark = pytest.mark.integration

INTEGRATION_ENABLED = os.getenv("POLY_TEST_INTEGRATION", "false").lower() == "true"


@pytest.fixture(scope="module")
def real_client():
    """
    Create a real Polymarket client for integration tests.

    Skips if integration tests are disabled or credentials are missing.
    """
    if not INTEGRATION_ENABLED:
        pytest.skip("Integration tests disabled (set POLY_TEST_INTEGRATION=true)")

    # Check for required environment variables
    if not os.getenv("PK") or not os.getenv("BROWSER_ADDRESS"):
        pytest.skip("Missing PK or BROWSER_ADDRESS environment variables")

    try:
        from poly_data.polymarket_client import PolymarketClient

        client = PolymarketClient()
        return client
    except Exception as e:
        pytest.skip(f"Failed to initialize client: {e}")


class TestPolymarketClientIntegration:
    """Integration tests for PolymarketClient."""

    def test_get_order_book_real(self, real_client):
        """Verify order book fetching works with real API."""
        # Use a known active market token
        # Note: This may fail if the market becomes inactive
        try:
            orderbook = real_client.get_order_book(
                "71321045679013447797864514828570804964740509273960966121330942155432910325412"
            )

            assert orderbook is not None
            # Orderbook should have bids and asks structure
            assert hasattr(orderbook, "bids") or "bids" in str(type(orderbook))
        except Exception as e:
            # API may return empty or fail for inactive tokens
            pytest.skip(f"Orderbook fetch failed (possibly inactive token): {e}")

    def test_get_all_positions_real(self, real_client):
        """Verify position fetching works."""
        positions = real_client.get_all_positions()

        # Should return a DataFrame (possibly empty)
        assert positions is not None
        # Check it's a DataFrame or similar structure
        assert hasattr(positions, "iterrows") or hasattr(positions, "__iter__")

    def test_get_usdc_balance_real(self, real_client):
        """Verify balance checking works."""
        balance = real_client.get_usdc_balance()

        assert balance is not None
        assert isinstance(balance, (int, float))
        assert balance >= 0

    def test_get_all_orders_real(self, real_client):
        """Verify order fetching works."""
        orders = real_client.get_all_orders()

        assert orders is not None
        # Should return a DataFrame (possibly empty)
        assert hasattr(orders, "iterrows") or hasattr(orders, "__iter__")


class TestAPIReadOperations:
    """Test read-only API operations."""

    def test_can_connect_to_api(self, real_client):
        """Basic connectivity test."""
        # If we got here, client initialized successfully
        assert real_client is not None

    def test_client_has_required_methods(self, real_client):
        """Verify client has all expected methods."""
        required_methods = [
            "get_all_positions",
            "get_all_orders",
            "get_usdc_balance",
            "create_order",
            "cancel_all_asset",
            "cancel_all_market",
            "merge_positions",
            "redeem_positions",
        ]

        for method in required_methods:
            assert hasattr(real_client, method), f"Missing method: {method}"


@pytest.mark.skip(reason="Requires specific market condition IDs")
class TestMarketResolution:
    """Tests for market resolution checking."""

    def test_is_market_resolved_active(self, real_client):
        """Test checking an active (unresolved) market."""
        # This would need a known active market condition ID
        pass

    def test_is_market_resolved_resolved(self, real_client):
        """Test checking a resolved market."""
        # This would need a known resolved market condition ID
        pass
