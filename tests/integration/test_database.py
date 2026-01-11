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


def has_db_config():
    """Check if database configuration is available."""
    return bool(os.getenv("DB_HOST"))


@pytest.fixture(scope="module")
def db_available():
    """Check if database is available for testing."""
    if not INTEGRATION_ENABLED:
        pytest.skip("Integration tests disabled (set POLY_TEST_INTEGRATION=true)")

    if not has_db_config():
        pytest.skip("Missing database configuration (DB_HOST)")

    # Try to import and verify connection works
    try:
        from db.supabase_client import get_db_connection
        with get_db_connection() as conn:
            # Connection successful
            pass
        return True
    except Exception as e:
        pytest.skip(f"Failed to connect to database: {e}")


class TestDatabaseIntegration:
    """Integration tests for database operations."""

    def test_connection_works(self, db_available):
        """Verify database connection is established."""
        from db.supabase_client import get_db_connection

        with get_db_connection() as conn:
            assert conn is not None
            # Verify it's a valid connection by checking it has cursor method
            assert hasattr(conn, 'cursor')

    def test_can_query_markets(self, db_available):
        """Test querying all_markets table."""
        from db.supabase_client import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM all_markets")
            row = cursor.fetchone()

        assert row is not None
        assert row['count'] >= 0  # Table exists and is queryable

    def test_can_query_selected_markets(self, db_available):
        """Test querying selected_markets table."""
        from db.supabase_client import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM selected_markets")
            row = cursor.fetchone()

        assert row is not None
        assert row['count'] >= 0

    def test_can_query_hyperparameters(self, db_available):
        """Test querying hyperparameters table."""
        from db.supabase_client import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT * FROM hyperparameters LIMIT 5")
            rows = cursor.fetchall()

        # Should have some hyperparameters defined
        assert rows is not None


class TestDatabaseCursor:
    """Test database cursor context manager."""

    def test_cursor_context_manager(self, db_available):
        """Test get_db_cursor context manager."""
        from db.supabase_client import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT 1 as value")
            result = cursor.fetchone()

        assert result['value'] == 1

    def test_cursor_returns_dict_rows(self, db_available):
        """Verify cursor returns RealDictCursor rows."""
        from db.supabase_client import get_db_cursor

        with get_db_cursor(commit=False) as cursor:
            cursor.execute("SELECT 1 as col_a, 2 as col_b")
            result = cursor.fetchone()

        # Should be able to access by column name
        assert result['col_a'] == 1
        assert result['col_b'] == 2


class TestDatabaseHelperFunctions:
    """Test higher-level database helper functions."""

    def test_get_all_markets(self, db_available):
        """Test get_all_markets function."""
        from db.supabase_client import get_all_markets

        df = get_all_markets()

        # Should return a DataFrame (possibly empty)
        assert df is not None
        assert hasattr(df, 'empty')  # Is a DataFrame

    def test_get_hyperparameters(self, db_available):
        """Test get_hyperparameters function."""
        from db.supabase_client import get_hyperparameters

        params = get_hyperparameters()

        assert params is not None
        assert isinstance(params, dict)

    def test_get_selected_markets(self, db_available):
        """Test get_selected_markets function."""
        from db.supabase_client import get_selected_markets

        df = get_selected_markets()

        assert df is not None
        assert hasattr(df, 'empty')  # Is a DataFrame

    def test_get_recent_trades(self, db_available):
        """Test get_recent_trades function."""
        from db.supabase_client import get_recent_trades

        df = get_recent_trades(limit=10)

        assert df is not None
        assert hasattr(df, 'empty')  # Is a DataFrame
