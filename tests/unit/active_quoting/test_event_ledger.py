"""
Unit tests for EventLedger - Append-only event log for gap detection and audit trail.

Tests:
- Initialization and database setup
- Event logging (fills, order updates, reconciliations)
- Gap detection
- Event retrieval and querying
- Summary statistics
- Thread safety basics
"""
import pytest
import tempfile
import os
from datetime import datetime, timezone
from unittest.mock import patch

from rebates.active_quoting.event_ledger import (
    EventLedger,
    EventType,
    LedgerEvent,
    GapInfo,
)


@pytest.fixture
def temp_db_path():
    """Create a temporary database path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Cleanup
    if os.path.exists(path):
        os.unlink(path)
    # Also cleanup WAL files if they exist
    for ext in ["-wal", "-shm"]:
        wal_path = path + ext
        if os.path.exists(wal_path):
            os.unlink(wal_path)


@pytest.fixture
def ledger(temp_db_path):
    """Create an EventLedger with a temporary database."""
    led = EventLedger(db_path=temp_db_path, enabled=True)
    yield led
    led.close()


@pytest.fixture
def memory_ledger():
    """Create an in-memory EventLedger."""
    led = EventLedger(db_path=None, enabled=True)
    yield led
    led.close()


@pytest.fixture
def disabled_ledger():
    """Create a disabled EventLedger."""
    led = EventLedger(enabled=False)
    yield led


# --- Initialization Tests ---

class TestInitialization:
    """Tests for EventLedger initialization."""

    def test_init_with_file_db(self, temp_db_path):
        """Test initialization with file-based database."""
        ledger = EventLedger(db_path=temp_db_path, enabled=True)
        assert ledger.is_enabled
        assert ledger.current_sequence == 0
        ledger.close()

    def test_init_with_memory_db(self, memory_ledger):
        """Test initialization with in-memory database."""
        assert memory_ledger.is_enabled
        assert memory_ledger.current_sequence == 0

    def test_init_disabled(self, disabled_ledger):
        """Test initialization with disabled ledger."""
        assert not disabled_ledger.is_enabled
        assert disabled_ledger.current_sequence == 0

    def test_init_resumes_sequence(self, temp_db_path):
        """Test that sequence number resumes from previous events."""
        # Create ledger and add some events
        led1 = EventLedger(db_path=temp_db_path, enabled=True)
        led1.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        led1.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        assert led1.current_sequence == 2
        led1.close()

        # Create new ledger - should resume from sequence 2
        led2 = EventLedger(db_path=temp_db_path, enabled=True)
        assert led2.current_sequence == 2
        led2.close()


# --- Event Logging Tests ---

class TestEventLogging:
    """Tests for logging events."""

    def test_log_fill(self, memory_ledger):
        """Test logging a fill event."""
        seq = memory_ledger.log_fill(
            order_id="order123",
            token_id="token456",
            side="BUY",
            price=0.55,
            size=100.0,
            fee=0.25,
            trade_id="trade789",
        )
        assert seq == 1
        assert memory_ledger.current_sequence == 1

    def test_log_order_update(self, memory_ledger):
        """Test logging an order update event."""
        seq = memory_ledger.log_order_update(
            order_id="order123",
            token_id="token456",
            side="BUY",
            status="CANCELLED",
            original_size=100.0,
            remaining_size=50.0,
        )
        assert seq == 1
        assert memory_ledger.current_sequence == 1

    def test_log_reconciliation(self, memory_ledger):
        """Test logging a reconciliation event."""
        seq = memory_ledger.log_reconciliation(
            open_orders_count=5,
            pending_buys_adjusted={"token1": -10.0, "token2": 5.0},
            source="api",
        )
        assert seq == 1
        assert memory_ledger.current_sequence == 1

    def test_log_generic_event(self, memory_ledger):
        """Test logging a generic event."""
        seq = memory_ledger.log_event(
            event_type=EventType.ORDER_UPDATE,
            payload={"custom": "data"},
            source="manual",
        )
        assert seq == 1

    def test_log_disabled_ledger(self, disabled_ledger):
        """Test that logging to disabled ledger returns None."""
        seq = disabled_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        assert seq is None

    def test_sequence_increments(self, memory_ledger):
        """Test that sequence numbers increment correctly."""
        seq1 = memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        seq2 = memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        seq3 = memory_ledger.log_order_update("o3", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        
        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3
        assert memory_ledger.current_sequence == 3


# --- Gap Detection Tests ---

class TestGapDetection:
    """Tests for WebSocket sequence gap detection."""

    def test_no_gap_sequential(self, memory_ledger):
        """Test no gap detected for sequential WS sequences."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=1)
        memory_ledger.log_event(EventType.FILL, {"data": 2}, ws_sequence=2)
        memory_ledger.log_event(EventType.FILL, {"data": 3}, ws_sequence=3)
        
        assert not memory_ledger.has_unresolved_gaps()

    def test_gap_detected(self, memory_ledger):
        """Test gap detected for non-sequential WS sequences."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=1)
        memory_ledger.log_event(EventType.FILL, {"data": 5}, ws_sequence=5)  # Gap: 2,3,4
        
        assert memory_ledger.has_unresolved_gaps()
        gaps = memory_ledger.get_unresolved_gaps()
        assert len(gaps) == 1
        assert gaps[0].expected_start == 2
        assert gaps[0].expected_end == 4
        assert gaps[0].gap_size == 3

    def test_multiple_gaps(self, memory_ledger):
        """Test detection of multiple gaps."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=1)
        memory_ledger.log_event(EventType.FILL, {"data": 5}, ws_sequence=5)  # Gap: 2,3,4
        memory_ledger.log_event(EventType.FILL, {"data": 10}, ws_sequence=10)  # Gap: 6,7,8,9
        
        assert memory_ledger.has_unresolved_gaps()
        gaps = memory_ledger.get_unresolved_gaps()
        assert len(gaps) == 2

    def test_clear_gaps(self, memory_ledger):
        """Test clearing gaps after reconciliation."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=1)
        memory_ledger.log_event(EventType.FILL, {"data": 5}, ws_sequence=5)
        
        assert memory_ledger.has_unresolved_gaps()
        
        memory_ledger.clear_gaps()
        
        assert not memory_ledger.has_unresolved_gaps()

    def test_gap_event_logged(self, memory_ledger):
        """Test that gap detection creates a GAP_DETECTED event."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=1)
        memory_ledger.log_event(EventType.FILL, {"data": 5}, ws_sequence=5)
        
        events = memory_ledger.get_events(event_type=EventType.GAP_DETECTED)
        assert len(events) == 1
        assert events[0].payload["gap_size"] == 3


# --- Event Retrieval Tests ---

class TestEventRetrieval:
    """Tests for retrieving events."""

    def test_get_all_events(self, memory_ledger):
        """Test getting all events."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        memory_ledger.log_order_update("o3", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        
        events = memory_ledger.get_events()
        assert len(events) == 3

    def test_get_events_since_sequence(self, memory_ledger):
        """Test getting events after a specific sequence."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        memory_ledger.log_fill("o3", "t1", "BUY", 0.55, 8.0)
        
        events = memory_ledger.get_events(since_sequence=1)
        assert len(events) == 2
        assert events[0].sequence_number == 2
        assert events[1].sequence_number == 3

    def test_get_events_by_type(self, memory_ledger):
        """Test filtering events by type."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        memory_ledger.log_order_update("o3", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        
        fills = memory_ledger.get_events(event_type=EventType.FILL)
        assert len(fills) == 2
        
        updates = memory_ledger.get_events(event_type=EventType.ORDER_UPDATE)
        assert len(updates) == 1

    def test_get_fills_since(self, memory_ledger):
        """Test getting fill events since a sequence."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_order_update("o2", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        memory_ledger.log_fill("o3", "t1", "SELL", 0.6, 5.0)
        
        fills = memory_ledger.get_fills_since(since_sequence=1)
        assert len(fills) == 1
        assert fills[0].payload["side"] == "SELL"

    def test_get_fills_by_token(self, memory_ledger):
        """Test filtering fills by token ID."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t2", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o3", "t1", "SELL", 0.6, 5.0)
        
        t1_fills = memory_ledger.get_fills_since(token_id="t1")
        assert len(t1_fills) == 2

    def test_get_order_updates_for_order(self, memory_ledger):
        """Test getting order updates for specific order."""
        memory_ledger.log_order_update("order1", "t1", "BUY", "OPEN", 10.0, 10.0)
        memory_ledger.log_order_update("order2", "t1", "BUY", "OPEN", 5.0, 5.0)
        memory_ledger.log_order_update("order1", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        
        updates = memory_ledger.get_order_updates_for_order("order1")
        assert len(updates) == 2

    def test_get_events_with_limit(self, memory_ledger):
        """Test limiting number of returned events."""
        for i in range(10):
            memory_ledger.log_fill(f"o{i}", "t1", "BUY", 0.5, 10.0)
        
        events = memory_ledger.get_events(limit=5)
        assert len(events) == 5

    def test_get_events_disabled_ledger(self, disabled_ledger):
        """Test getting events from disabled ledger returns empty list."""
        events = disabled_ledger.get_events()
        assert events == []


# --- Summary and Statistics Tests ---

class TestSummaryStatistics:
    """Tests for summary and statistics."""

    def test_get_event_count(self, memory_ledger):
        """Test getting total event count."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        
        assert memory_ledger.get_event_count() == 2

    def test_get_event_count_empty(self, memory_ledger):
        """Test event count for empty ledger."""
        assert memory_ledger.get_event_count() == 0

    def test_get_event_count_disabled(self, disabled_ledger):
        """Test event count for disabled ledger."""
        assert disabled_ledger.get_event_count() == 0

    def test_get_summary(self, memory_ledger):
        """Test getting summary statistics."""
        memory_ledger.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        memory_ledger.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        memory_ledger.log_order_update("o3", "t1", "BUY", "CANCELLED", 10.0, 10.0)
        
        summary = memory_ledger.get_summary()
        
        assert summary["enabled"] is True
        assert summary["current_sequence"] == 3
        assert summary["total_events"] == 3
        assert summary["events_by_type"]["FILL"] == 2
        assert summary["events_by_type"]["ORDER_UPDATE"] == 1
        assert summary["unresolved_gaps"] == 0

    def test_get_summary_disabled(self, disabled_ledger):
        """Test summary for disabled ledger."""
        summary = disabled_ledger.get_summary()
        assert summary["enabled"] is False


# --- LedgerEvent Model Tests ---

class TestLedgerEventModel:
    """Tests for LedgerEvent dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        event = LedgerEvent(
            sequence_number=1,
            event_type=EventType.FILL,
            timestamp=datetime(2024, 1, 14, 12, 0, 0, tzinfo=timezone.utc),
            payload={"order_id": "test123"},
            source="websocket",
        )
        
        d = event.to_dict()
        
        assert d["sequence_number"] == 1
        assert d["event_type"] == "FILL"
        assert d["timestamp"] == "2024-01-14T12:00:00+00:00"
        assert d["payload"] == {"order_id": "test123"}
        assert d["source"] == "websocket"

    def test_from_dict(self):
        """Test creation from dictionary."""
        d = {
            "sequence_number": 5,
            "event_type": "ORDER_UPDATE",
            "timestamp": "2024-01-14T12:00:00+00:00",
            "payload": {"status": "CANCELLED"},
            "source": "api",
        }
        
        event = LedgerEvent.from_dict(d)
        
        assert event.sequence_number == 5
        assert event.event_type == EventType.ORDER_UPDATE
        assert event.payload == {"status": "CANCELLED"}
        assert event.source == "api"


# --- Thread Safety Tests ---

class TestThreadSafety:
    """Basic thread safety tests."""

    def test_concurrent_logging(self, memory_ledger):
        """Test concurrent logging from multiple threads."""
        import threading
        import time
        
        results = []
        errors = []
        
        def log_events(thread_id, count):
            try:
                for i in range(count):
                    seq = memory_ledger.log_fill(
                        f"order_{thread_id}_{i}",
                        "token1",
                        "BUY",
                        0.5,
                        10.0,
                    )
                    results.append(seq)
            except Exception as e:
                errors.append(e)
        
        threads = []
        for i in range(5):
            t = threading.Thread(target=log_events, args=(i, 10))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 50
        # All sequences should be unique
        assert len(set(results)) == 50


# --- Edge Cases ---

class TestEdgeCases:
    """Tests for edge cases."""

    def test_log_with_none_trade_id(self, memory_ledger):
        """Test logging fill with None trade_id."""
        seq = memory_ledger.log_fill(
            order_id="o1",
            token_id="t1",
            side="BUY",
            price=0.5,
            size=10.0,
            trade_id=None,
        )
        assert seq == 1
        
        events = memory_ledger.get_events()
        assert events[0].payload["trade_id"] is None

    def test_log_empty_pending_buys_adjusted(self, memory_ledger):
        """Test reconciliation with empty adjustments."""
        seq = memory_ledger.log_reconciliation(
            open_orders_count=0,
            pending_buys_adjusted={},
        )
        assert seq == 1

    def test_ws_sequence_first_event(self, memory_ledger):
        """Test that first WS event doesn't trigger gap."""
        memory_ledger.log_event(EventType.FILL, {"data": 1}, ws_sequence=100)
        
        # No gap should be detected for first event
        assert not memory_ledger.has_unresolved_gaps()

    def test_persistence_after_close_reopen(self, temp_db_path):
        """Test that events persist after close and reopen."""
        # Create ledger and log events
        led1 = EventLedger(db_path=temp_db_path, enabled=True)
        led1.log_fill("o1", "t1", "BUY", 0.5, 10.0)
        led1.log_fill("o2", "t1", "SELL", 0.6, 5.0)
        led1.close()
        
        # Reopen and verify events are still there
        led2 = EventLedger(db_path=temp_db_path, enabled=True)
        events = led2.get_events()
        assert len(events) == 2
        assert events[0].payload["order_id"] == "o1"
        led2.close()
