"""
Integration test configuration.

These tests connect to REAL services:
- Polymarket API (read-only operations)
- PostgreSQL database
- Telegram (test bot/channel)

NEVER run against production credentials without understanding the risks.
"""

import pytest
import os

# Check if integration tests should run
INTEGRATION_ENABLED = os.getenv("POLY_TEST_INTEGRATION", "false").lower() == "true"


def pytest_configure(config):
    """Configure pytest for integration tests."""
    config.addinivalue_line("markers", "integration: Integration tests (require real services)")


def pytest_collection_modifyitems(config, items):
    """Skip integration tests if not enabled."""
    if INTEGRATION_ENABLED:
        return

    skip_integration = pytest.mark.skip(
        reason="Integration tests disabled (set POLY_TEST_INTEGRATION=true)"
    )

    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
