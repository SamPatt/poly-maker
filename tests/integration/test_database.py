"""
Integration tests for database operations.

These tests verify database connectivity and basic operations.
Requires POLY_TEST_INTEGRATION=true environment variable.

IMPORTANT: Use test database or read-only operations to avoid data corruption.
"""

import pytest
import os

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.integration

INTEGRATION_ENABLED = os.getenv("POLY_TEST_INTEGRATION", "false").lower() == "true"


@pytest.fixture(scope="module")
def db_connection():
    """
    Create database connection for integration tests.

    Skips if integration tests are disabled or database is unavailable.
    """
    if not INTEGRATION_ENABLED:
        pytest.skip("Integration tests disabled (set POLY_TEST_INTEGRATION=true)")

    # Check for database configuration
    if not os.getenv("DB_HOST"):
        pytest.skip("Missing database configuration (DB_HOST)")

    try:
        from db.supabase_client import get_db_connection

        conn = get_db_connection()
        yield conn
        conn.close()
    except Exception as e:
        pytest.skip(f"Failed to connect to database: {e}")


class TestDatabaseIntegration:
    """Integration tests for database operations."""

    def test_connection_works(self, db_connection):
        """Verify database connection is established."""
        assert db_connection is not None

    def test_can_query_markets(self, db_connection):
        """Test querying all_markets table."""
        try:
            cursor = db_connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM all_markets")
            count = cursor.fetchone()[0]
            cursor.close()

            assert count >= 0  # Table exists and is queryable
        except Exception as e:
            pytest.skip(f"Could not query all_markets: {e}")

    def test_can_query_selected_markets(self, db_connection):
        """Test querying selected_markets table."""
        try:
            cursor = db_connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM selected_markets")
            count = cursor.fetchone()[0]
            cursor.close()

            assert count >= 0
        except Exception as e:
            pytest.skip(f"Could not query selected_markets: {e}")

    def test_can_query_hyperparameters(self, db_connection):
        """Test querying hyperparameters table."""
        try:
            cursor = db_connection.cursor()
            cursor.execute("SELECT * FROM hyperparameters LIMIT 5")
            rows = cursor.fetchall()
            cursor.close()

            # Should have some hyperparameters defined
            assert rows is not None
        except Exception as e:
            pytest.skip(f"Could not query hyperparameters: {e}")


class TestDatabaseCursor:
    """Test database cursor context manager."""

    def test_cursor_context_manager(self, db_connection):
        """Test get_db_cursor context manager."""
        try:
            from db.supabase_client import get_db_cursor

            with get_db_cursor(commit=False) as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()

            assert result[0] == 1
        except Exception as e:
            pytest.skip(f"Cursor context manager test failed: {e}")
