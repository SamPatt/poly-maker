"""
Unit tests for Active Quoting Persistence module.

Tests:
- Position persistence
- Fill recording
- Markout storage
- Session management
- Graceful error handling
"""
import pytest
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from rebates.active_quoting.persistence import (
    ActiveQuotingPersistence,
    PersistenceConfig,
    create_persistence,
)
from rebates.active_quoting.models import Position, Fill, OrderSide
from rebates.active_quoting.fill_analytics import FillRecord, MarkoutSample


# --- PersistenceConfig Tests ---

class TestPersistenceConfig:
    """Tests for PersistenceConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = PersistenceConfig()
        assert config.enabled is True
        assert config.save_positions is True
        assert config.save_fills is True
        assert config.save_markouts is True
        assert config.save_sessions is True

    def test_disabled_config(self):
        """Test disabled configuration."""
        config = PersistenceConfig(enabled=False)
        assert config.enabled is False

    def test_selective_config(self):
        """Test selective feature disabling."""
        config = PersistenceConfig(
            enabled=True,
            save_positions=True,
            save_fills=False,
            save_markouts=False,
            save_sessions=True,
        )
        assert config.save_fills is False
        assert config.save_markouts is False
        assert config.save_positions is True


# --- ActiveQuotingPersistence Tests ---

class TestPersistenceInitialization:
    """Tests for persistence initialization."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_init_with_db_available(self, mock_check):
        """Test initialization when DB is available."""
        mock_check.return_value = True

        persistence = ActiveQuotingPersistence()

        assert persistence.is_enabled is True
        assert persistence.session_id is None

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_init_with_db_unavailable(self, mock_check):
        """Test initialization when DB is unavailable."""
        mock_check.return_value = False

        persistence = ActiveQuotingPersistence()

        assert persistence.is_enabled is False

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_init_disabled_by_config(self, mock_check):
        """Test initialization with disabled config."""
        mock_check.return_value = True

        config = PersistenceConfig(enabled=False)
        persistence = ActiveQuotingPersistence(config)

        assert persistence.is_enabled is False


# --- Session Management Tests ---

class TestSessionManagement:
    """Tests for session management."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_start_session_generates_id(self, mock_check):
        """Test starting session generates unique ID."""
        mock_check.return_value = False  # DB not available

        persistence = ActiveQuotingPersistence()
        session_id = persistence.start_session(["token_1", "token_2"])

        assert session_id is not None
        assert len(session_id) > 0
        assert persistence.session_id == session_id

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_session")
    def test_start_session_saves_to_db(self, mock_save, mock_check):
        """Test starting session saves to database."""
        mock_check.return_value = True
        mock_save.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True
        config_snapshot = {"order_size": 25.0}

        session_id = persistence.start_session(
            token_ids=["token_1"],
            config_snapshot=config_snapshot,
        )

        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert "token_1" in call_kwargs["markets"]
        assert call_kwargs["config_snapshot"] == config_snapshot

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.update_active_quoting_session")
    def test_end_session(self, mock_update, mock_check):
        """Test ending session updates database."""
        mock_check.return_value = True
        mock_update.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True
        persistence._session_id = "test_session"

        result = persistence.end_session(
            status="STOPPED",
            stats={"total_fills": 10},
        )

        assert result is True
        mock_update.assert_called_once()

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_end_session_without_session_id(self, mock_check):
        """Test ending session without active session is no-op."""
        mock_check.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True
        # No session_id set

        result = persistence.end_session()

        assert result is True  # No-op succeeds


# --- Position Persistence Tests ---

class TestPositionPersistence:
    """Tests for position persistence."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_position")
    def test_save_position(self, mock_save, mock_check):
        """Test saving a position."""
        mock_check.return_value = True
        mock_save.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        position = Position(
            token_id="test_token",
            size=100.0,
            avg_entry_price=0.50,
            realized_pnl=5.0,
            total_fees_paid=0.10,
        )

        result = persistence.save_position(position, market_name="BTC Up")

        assert result is True
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["token_id"] == "test_token"
        assert call_kwargs["size"] == 100.0
        assert call_kwargs["market_name"] == "BTC Up"

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_active_quoting_positions")
    def test_load_positions(self, mock_get, mock_check):
        """Test loading positions."""
        mock_check.return_value = True
        mock_get.return_value = pd.DataFrame([
            {
                "token_id": "token_1",
                "market_name": "BTC Up",
                "size": 50.0,
                "avg_price": 0.45,
                "realized_pnl": 2.0,
                "total_fees": 0.05,
            }
        ])

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        positions = persistence.load_positions()

        assert len(positions) == 1
        assert "token_1" in positions
        pos = positions["token_1"]
        assert pos.size == 50.0
        assert pos.avg_entry_price == 0.45
        assert pos.realized_pnl == 2.0

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_active_quoting_positions")
    def test_load_positions_empty(self, mock_get, mock_check):
        """Test loading positions when none exist."""
        mock_check.return_value = True
        mock_get.return_value = pd.DataFrame()

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        positions = persistence.load_positions()

        assert len(positions) == 0

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_save_position_disabled(self, mock_check):
        """Test saving position when disabled is no-op."""
        mock_check.return_value = True

        config = PersistenceConfig(enabled=True, save_positions=False)
        persistence = ActiveQuotingPersistence(config)
        persistence._db_available = True

        position = Position(token_id="test")
        result = persistence.save_position(position)

        assert result is True  # No-op succeeds


# --- Fill Persistence Tests ---

class TestFillPersistence:
    """Tests for fill persistence."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_fill")
    def test_save_fill(self, mock_save, mock_check):
        """Test saving a fill."""
        mock_check.return_value = True
        mock_save.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        fill = Fill(
            order_id="order_123",
            token_id="test_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            fee=-0.01,
            trade_id="trade_456",
        )

        result = persistence.save_fill(fill, mid_at_fill=0.495, market_name="BTC Up")

        assert result is True
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["token_id"] == "test_token"
        assert call_kwargs["side"] == "BUY"
        assert call_kwargs["price"] == 0.50
        assert call_kwargs["mid_at_fill"] == 0.495

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_fill")
    @patch("db.pg_client.save_active_quoting_markout")
    def test_save_fill_record(self, mock_markout, mock_fill, mock_check):
        """Test saving a FillRecord with markouts."""
        mock_check.return_value = True
        mock_fill.return_value = True
        mock_markout.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        fill = Fill(
            order_id="order_123",
            token_id="test_token",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
            trade_id="trade_456",
        )
        record = FillRecord(fill=fill, mid_price_at_fill=0.50)
        record.markouts[5] = MarkoutSample(
            fill_id="trade_456",
            horizon_seconds=5,
            mid_at_fill=0.50,
        )
        record.markouts[15] = MarkoutSample(
            fill_id="trade_456",
            horizon_seconds=15,
            mid_at_fill=0.50,
        )

        result = persistence.save_fill_record(record)

        assert result is True
        mock_fill.assert_called_once()
        assert mock_markout.call_count == 2  # Two horizons


# --- Markout Persistence Tests ---

class TestMarkoutPersistence:
    """Tests for markout persistence."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_markout")
    def test_save_markout(self, mock_save, mock_check):
        """Test saving a markout sample."""
        mock_check.return_value = True
        mock_save.return_value = True

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        sample = MarkoutSample(
            fill_id="fill_123",
            horizon_seconds=5,
            mid_at_fill=0.50,
            mid_at_horizon=0.52,
            markout=0.02,
            markout_bps=400.0,
        )

        result = persistence.save_markout(sample)

        assert result is True
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args[1]
        assert call_kwargs["fill_id"] == "fill_123"
        assert call_kwargs["markout"] == 0.02
        assert call_kwargs["markout_bps"] == 400.0

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_pending_markout_captures")
    def test_load_pending_markouts(self, mock_get, mock_check):
        """Test loading pending markout captures."""
        mock_check.return_value = True
        mock_get.return_value = pd.DataFrame([
            {
                "fill_id": "fill_1",
                "token_id": "token_1",
                "horizon_seconds": 5,
                "mid_at_fill": 0.50,
            },
            {
                "fill_id": "fill_2",
                "token_id": "token_1",
                "horizon_seconds": 15,
                "mid_at_fill": 0.51,
            },
        ])

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        pending = persistence.load_pending_markouts()

        assert len(pending) == 2
        assert pending[0]["fill_id"] == "fill_1"


# --- Error Handling Tests ---

class TestErrorHandling:
    """Tests for graceful error handling."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_position")
    def test_save_position_handles_error(self, mock_save, mock_check):
        """Test save_position handles exceptions gracefully."""
        mock_check.return_value = True
        mock_save.side_effect = Exception("Database error")

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        position = Position(token_id="test")
        result = persistence.save_position(position)

        assert result is False  # Should return False, not raise

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_active_quoting_positions")
    def test_load_positions_handles_error(self, mock_get, mock_check):
        """Test load_positions handles exceptions gracefully."""
        mock_check.return_value = True
        mock_get.side_effect = Exception("Database error")

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        positions = persistence.load_positions()

        assert positions == {}  # Should return empty dict, not raise

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.save_active_quoting_fill")
    def test_save_fill_handles_error(self, mock_save, mock_check):
        """Test save_fill handles exceptions gracefully."""
        mock_check.return_value = True
        mock_save.side_effect = Exception("Database error")

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        fill = Fill(
            order_id="order_1",
            token_id="token_1",
            side=OrderSide.BUY,
            price=0.50,
            size=10.0,
        )
        result = persistence.save_fill(fill)

        assert result is False


# --- Analytics Queries Tests ---

class TestAnalyticsQueries:
    """Tests for analytics query methods."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_active_quoting_fills")
    def test_get_fill_history(self, mock_get, mock_check):
        """Test getting fill history."""
        mock_check.return_value = True
        mock_get.return_value = pd.DataFrame([
            {"fill_id": "fill_1", "price": 0.50},
            {"fill_id": "fill_2", "price": 0.51},
        ])

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        history = persistence.get_fill_history(token_id="token_1", limit=50)

        assert len(history) == 2
        mock_get.assert_called_once_with(token_id="token_1", limit=50)

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.get_active_quoting_markout_stats")
    def test_get_markout_stats(self, mock_get, mock_check):
        """Test getting markout statistics."""
        mock_check.return_value = True
        mock_get.return_value = pd.DataFrame([
            {"horizon_seconds": 5, "avg_markout_bps": 25.0},
            {"horizon_seconds": 15, "avg_markout_bps": -10.0},
        ])

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        stats = persistence.get_markout_stats()

        assert len(stats) == 2
        assert stats[5] == 25.0
        assert stats[15] == -10.0


# --- Cleanup Tests ---

class TestCleanup:
    """Tests for cleanup functionality."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    @patch("db.pg_client.cleanup_old_active_quoting_data")
    def test_cleanup_old_data(self, mock_cleanup, mock_check):
        """Test cleaning up old data."""
        mock_check.return_value = True
        mock_cleanup.return_value = {"fills": 10, "sessions": 2}

        persistence = ActiveQuotingPersistence()
        persistence._db_available = True

        result = persistence.cleanup_old_data(days=30)

        assert result["fills"] == 10
        assert result["sessions"] == 2
        mock_cleanup.assert_called_once_with(days=30)


# --- Factory Function Tests ---

class TestCreatePersistence:
    """Tests for create_persistence factory function."""

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_create_persistence_enabled(self, mock_check):
        """Test creating enabled persistence."""
        mock_check.return_value = True

        persistence = create_persistence(enabled=True)

        assert persistence.config.enabled is True

    @patch("rebates.active_quoting.persistence._check_db_available")
    def test_create_persistence_disabled(self, mock_check):
        """Test creating disabled persistence."""
        mock_check.return_value = True

        persistence = create_persistence(enabled=False)

        assert persistence.config.enabled is False
