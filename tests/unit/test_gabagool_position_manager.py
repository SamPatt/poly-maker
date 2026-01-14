"""
Unit tests for rebates/gabagool/position_manager.py

Tests:
- PositionManager: Position lifecycle management
- PositionSummary: Summary generation
- Persistence: Save/load positions
"""

import asyncio
import pytest
import tempfile
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from rebates.gabagool.position_manager import PositionManager, PositionSummary
from rebates.gabagool.executor import TrackedPosition


def run_async(coro):
    """Helper to run async coroutines in sync tests."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def make_position(
    position_id: str = "test-pos-1",
    up_filled: float = 50,
    down_filled: float = 50,
    is_balanced: bool = None,
) -> TrackedPosition:
    """Create a test position."""
    if is_balanced is None:
        is_balanced = abs(up_filled - down_filled) < 0.01

    return TrackedPosition(
        id=position_id,
        market_slug="test-market",
        condition_id="0xcond123",
        up_token="token_up",
        down_token="token_down",
        neg_risk=False,
        entry_time=datetime.now(timezone.utc),
        up_entry_price=0.48,
        down_entry_price=0.49,
        combined_cost=0.97,
        target_size=50,
        up_filled=up_filled,
        down_filled=down_filled,
        is_balanced=is_balanced,
    )


class TestPositionManagerInit:
    """Tests for PositionManager initialization."""

    def test_initializes_with_defaults(self):
        """Should initialize with default values."""
        manager = PositionManager()

        assert manager.client is None
        assert len(manager.positions) == 0
        assert manager.total_profit == 0.0

    def test_accepts_custom_client(self):
        """Should accept custom client."""
        mock_client = MagicMock()
        manager = PositionManager(client=mock_client)

        assert manager.client == mock_client


class TestAddPosition:
    """Tests for adding positions."""

    def test_adds_position(self):
        """Should add position to tracking."""
        manager = PositionManager()
        position = make_position()

        manager.add_position(position)

        assert position.id in manager.positions
        assert manager.total_entries == 1

    def test_multiple_positions(self):
        """Should track multiple positions."""
        manager = PositionManager()

        manager.add_position(make_position("pos-1"))
        manager.add_position(make_position("pos-2"))
        manager.add_position(make_position("pos-3"))

        assert len(manager.positions) == 3
        assert manager.total_entries == 3


class TestGetPositions:
    """Tests for position retrieval."""

    def test_get_position_by_id(self):
        """Should retrieve position by ID."""
        manager = PositionManager()
        position = make_position("test-id")
        manager.add_position(position)

        retrieved = manager.get_position("test-id")

        assert retrieved == position

    def test_get_nonexistent_position(self):
        """Should return None for nonexistent position."""
        manager = PositionManager()

        retrieved = manager.get_position("nonexistent")

        assert retrieved is None

    def test_get_active_positions(self):
        """Should return only non-closed positions."""
        manager = PositionManager()
        pos1 = make_position("pos-1")
        pos2 = make_position("pos-2")
        pos2.is_closed = True

        manager.add_position(pos1)
        manager.add_position(pos2)

        active = manager.get_active_positions()

        assert len(active) == 1
        assert active[0].id == "pos-1"

    def test_get_merge_ready_positions(self):
        """Should return only balanced non-closed positions."""
        manager = PositionManager()
        pos1 = make_position("pos-1", up_filled=50, down_filled=50, is_balanced=True)
        pos2 = make_position("pos-2", up_filled=50, down_filled=40, is_balanced=False)
        pos3 = make_position("pos-3", up_filled=50, down_filled=50, is_balanced=True)
        pos3.is_closed = True

        manager.add_position(pos1)
        manager.add_position(pos2)
        manager.add_position(pos3)

        merge_ready = manager.get_merge_ready_positions()

        assert len(merge_ready) == 1
        assert merge_ready[0].id == "pos-1"


class TestUpdatePositionFills:
    """Tests for updating position fills."""

    def test_updates_fills(self):
        """Should update fill amounts."""
        manager = PositionManager()
        position = make_position("pos-1", up_filled=30, down_filled=40, is_balanced=False)
        manager.add_position(position)

        run_async(manager.update_position_fills("pos-1", 50, 50))

        assert position.up_filled == 50
        assert position.down_filled == 50
        assert position.is_balanced is True

    def test_handles_nonexistent_position(self):
        """Should handle updating nonexistent position."""
        manager = PositionManager()

        # Should not raise
        run_async(manager.update_position_fills("nonexistent", 50, 50))


class TestProcessMerges:
    """Tests for merge processing."""

    def test_merges_ready_positions_dry_run(self):
        """Should process merges in dry run mode."""
        with patch("rebates.gabagool.position_manager.config") as mock_config:
            mock_config.DRY_RUN = True

            manager = PositionManager()
            pos1 = make_position("pos-1", is_balanced=True)
            manager.add_position(pos1)

            count = run_async(manager.process_merges())

            assert count == 1
            assert pos1.is_closed is True

    def test_skips_unbalanced_positions(self):
        """Should skip unbalanced positions."""
        manager = PositionManager()
        pos1 = make_position("pos-1", up_filled=50, down_filled=40, is_balanced=False)
        manager.add_position(pos1)

        count = run_async(manager.process_merges())

        assert count == 0
        assert pos1.is_closed is False

    def test_calls_client_merge(self):
        """Should call client.merge_positions with correct params."""
        with patch("rebates.gabagool.position_manager.config") as mock_config:
            mock_config.DRY_RUN = False

            mock_client = MagicMock()
            mock_client.merge_positions.return_value = "0xtxhash"

            manager = PositionManager(client=mock_client)
            pos1 = make_position("pos-1", is_balanced=True)
            manager.add_position(pos1)

            count = run_async(manager.process_merges())

            assert count == 1
            mock_client.merge_positions.assert_called_once()


class TestCheckResolutions:
    """Tests for resolution checking."""

    def test_redeems_resolved_markets(self):
        """Should redeem positions when market resolved."""
        with patch("rebates.gabagool.position_manager.config") as mock_config:
            mock_config.DRY_RUN = True

            mock_client = MagicMock()
            mock_client.is_market_resolved.return_value = (True, "0xcond123")

            manager = PositionManager(client=mock_client)
            pos1 = make_position("pos-1")
            manager.add_position(pos1)

            count = run_async(manager.check_resolutions())

            assert count == 1
            assert pos1.is_closed is True

    def test_skips_unresolved_markets(self):
        """Should skip positions in unresolved markets."""
        mock_client = MagicMock()
        mock_client.is_market_resolved.return_value = (False, None)

        manager = PositionManager(client=mock_client)
        pos1 = make_position("pos-1")
        manager.add_position(pos1)

        count = run_async(manager.check_resolutions())

        assert count == 0
        assert pos1.is_closed is False


class TestClosePosition:
    """Tests for position closing."""

    def test_calculates_profit(self):
        """Should calculate profit on close."""
        manager = PositionManager()
        pos1 = make_position("pos-1", is_balanced=True)
        pos1.combined_cost = 0.97  # Entry cost
        pos1.up_filled = 50
        pos1.down_filled = 50
        manager.add_position(pos1)

        manager._close_position(pos1, "merged")

        # Profit = (50 * 1.0) - (50 * 0.97) = 50 - 48.5 = 1.5
        assert abs(pos1.realized_profit - 1.5) < 0.01
        assert manager.total_profit > 0

    def test_moves_to_closed_list(self):
        """Should move position to closed list."""
        manager = PositionManager()
        pos1 = make_position("pos-1")
        manager.add_position(pos1)

        manager._close_position(pos1, "merged")

        assert "pos-1" not in manager.positions
        assert len(manager.closed_positions) == 1


class TestGetSummary:
    """Tests for summary generation."""

    def test_returns_complete_summary(self):
        """Should return all summary fields."""
        manager = PositionManager()

        pos1 = make_position("pos-1", is_balanced=True)
        manager.add_position(pos1)

        # Set values after adding position (add_position increments total_entries)
        manager.total_entries = 10
        manager.total_merges = 5
        manager.total_profit = 15.50

        summary = manager.get_summary()

        assert summary["active_positions"] == 1
        assert summary["merge_ready"] == 1
        assert summary["total_entries"] == 10
        assert summary["total_merges"] == 5
        assert summary["total_profit"] == 15.50


class TestPositionSummary:
    """Tests for PositionSummary dataclass."""

    def test_creates_summary(self):
        """Should create position summary."""
        summary = PositionSummary(
            position_id="pos-1",
            market_slug="test-market",
            status="balanced",
            entry_cost=48.50,
            current_value=50.00,
            realized_pnl=0.0,
            unrealized_pnl=1.50,
            is_profitable=True,
        )

        assert summary.position_id == "pos-1"
        assert summary.is_profitable is True


class TestPersistence:
    """Tests for position persistence."""

    def test_save_and_load_positions(self):
        """Should save and load positions correctly."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_file = f.name

        try:
            # Create manager and add positions
            manager1 = PositionManager(positions_file=temp_file)
            pos1 = make_position("pos-1")
            pos2 = make_position("pos-2")
            manager1.add_position(pos1)
            manager1.add_position(pos2)
            manager1.total_profit = 10.50
            manager1._save_positions()  # Explicitly save after setting profit

            # Create new manager and load
            manager2 = PositionManager(positions_file=temp_file)
            count = manager2.load_positions()

            assert count == 2
            assert "pos-1" in manager2.positions
            assert "pos-2" in manager2.positions
            assert manager2.total_profit == 10.50

        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    def test_load_nonexistent_file(self):
        """Should handle nonexistent file gracefully."""
        manager = PositionManager(positions_file="/nonexistent/path.json")

        count = manager.load_positions()

        assert count == 0


class TestGetPositionSummaries:
    """Tests for position summary list."""

    def test_generates_summaries_for_all_positions(self):
        """Should generate summaries for all active positions."""
        manager = PositionManager()
        manager.add_position(make_position("pos-1"))
        manager.add_position(make_position("pos-2"))

        summaries = manager.get_position_summaries()

        assert len(summaries) == 2
        assert all(isinstance(s, PositionSummary) for s in summaries)
