"""
Event Ledger for Active Quoting Bot.

Provides an append-only event log for fills and order updates.
Enables:
- Gap detection (missing sequence numbers indicate dropped WebSocket messages)
- Recovery/replay capability
- Audit trail for debugging position discrepancies

Uses SQLite for simple, self-contained storage.
"""
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events logged to the ledger."""
    ORDER_UPDATE = "ORDER_UPDATE"
    FILL = "FILL"
    RECONCILIATION = "RECONCILIATION"
    GAP_DETECTED = "GAP_DETECTED"


@dataclass
class LedgerEvent:
    """A single event in the ledger."""
    sequence_number: int
    event_type: EventType
    timestamp: datetime
    payload: Dict[str, Any]
    source: str = "websocket"  # websocket, api, manual

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "sequence_number": self.sequence_number,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LedgerEvent":
        """Create from dictionary."""
        return cls(
            sequence_number=data["sequence_number"],
            event_type=EventType(data["event_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            payload=data["payload"],
            source=data.get("source", "websocket"),
        )


@dataclass
class GapInfo:
    """Information about a detected gap in sequence numbers."""
    expected_start: int
    expected_end: int
    actual_next: int
    gap_size: int
    detected_at: datetime


class EventLedger:
    """
    Append-only event ledger for tracking order updates and fills.
    
    Thread-safe implementation using SQLite with WAL mode for
    concurrent reads during writes.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        enabled: bool = True,
        max_events: int = 100000,  # Max events before rotation
    ):
        """
        Initialize the event ledger.
        
        Args:
            db_path: Path to SQLite database file. If None, uses in-memory DB.
            enabled: Whether ledger is enabled
            max_events: Maximum events before considering rotation
        """
        self._enabled = enabled
        self._max_events = max_events
        self._lock = threading.Lock()
        self._sequence_number = 0
        self._gaps: List[GapInfo] = []
        
        # Track last seen WebSocket sequence per source
        self._last_ws_sequence: Dict[str, int] = {}
        
        if not enabled:
            self._conn = None
            return
        
        # Determine database path
        if db_path is None:
            # In-memory database for testing
            self._db_path = ":memory:"
        else:
            self._db_path = db_path
            # Ensure directory exists
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize the SQLite database."""
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,  # We handle threading ourselves
                isolation_level=None,  # Autocommit mode
            )
            
            # Enable WAL mode for better concurrent access
            if self._db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            
            # Create events table
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence_number INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'websocket',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create index for sequence number queries
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_sequence 
                ON events(sequence_number)
            """)
            
            # Create index for event type queries
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_type 
                ON events(event_type)
            """)
            
            # Create gaps table to track detected gaps
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    expected_start INTEGER NOT NULL,
                    expected_end INTEGER NOT NULL,
                    actual_next INTEGER NOT NULL,
                    gap_size INTEGER NOT NULL,
                    detected_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)
            
            # Load current sequence number from DB
            cursor = self._conn.execute(
                "SELECT MAX(sequence_number) FROM events"
            )
            row = cursor.fetchone()
            if row and row[0] is not None:
                self._sequence_number = row[0]
            
            logger.info(
                f"Event ledger initialized at {self._db_path}, "
                f"current sequence: {self._sequence_number}"
            )
            
        except Exception as e:
            logger.error(f"Failed to initialize event ledger: {e}")
            self._enabled = False
            self._conn = None
    
    @property
    def is_enabled(self) -> bool:
        """Check if ledger is enabled."""
        return self._enabled and self._conn is not None
    
    @property
    def current_sequence(self) -> int:
        """Get current sequence number."""
        return self._sequence_number
    
    def log_event(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        source: str = "websocket",
        ws_sequence: Optional[int] = None,
    ) -> Optional[int]:
        """
        Log an event to the ledger.
        
        Args:
            event_type: Type of event
            payload: Event data
            source: Source of event (websocket, api, manual)
            ws_sequence: Optional WebSocket sequence number for gap detection
            
        Returns:
            Sequence number assigned to this event, or None if disabled
        """
        if not self.is_enabled:
            return None
        
        with self._lock:
            # Check for gaps in WebSocket sequence if provided
            if ws_sequence is not None and source == "websocket":
                self._check_ws_sequence_gap(ws_sequence, source)
            
            # Increment our sequence number
            self._sequence_number += 1
            seq = self._sequence_number
            
            try:
                self._conn.execute(
                    """
                    INSERT INTO events (sequence_number, event_type, timestamp, payload, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        seq,
                        event_type.value,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(payload),
                        source,
                    )
                )
                return seq
                
            except Exception as e:
                logger.error(f"Failed to log event: {e}")
                return None
    
    def _check_ws_sequence_gap(self, ws_sequence: int, source: str) -> None:
        """
        Check for gaps in WebSocket sequence numbers.
        
        Args:
            ws_sequence: The WebSocket sequence number
            source: Source identifier for tracking
        """
        key = f"ws_{source}"
        last_seq = self._last_ws_sequence.get(key)
        
        if last_seq is not None and ws_sequence > last_seq + 1:
            gap_size = ws_sequence - last_seq - 1
            gap = GapInfo(
                expected_start=last_seq + 1,
                expected_end=ws_sequence - 1,
                actual_next=ws_sequence,
                gap_size=gap_size,
                detected_at=datetime.now(timezone.utc),
            )
            self._gaps.append(gap)
            
            # Log gap to database
            try:
                self._conn.execute(
                    """
                    INSERT INTO gaps (expected_start, expected_end, actual_next, gap_size, detected_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        gap.expected_start,
                        gap.expected_end,
                        gap.actual_next,
                        gap.gap_size,
                        gap.detected_at.isoformat(),
                    )
                )
            except Exception as e:
                logger.error(f"Failed to log gap: {e}")
            
            # Log event for gap detection
            self._sequence_number += 1
            try:
                self._conn.execute(
                    """
                    INSERT INTO events (sequence_number, event_type, timestamp, payload, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self._sequence_number,
                        EventType.GAP_DETECTED.value,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps({
                            "expected_start": gap.expected_start,
                            "expected_end": gap.expected_end,
                            "actual_next": gap.actual_next,
                            "gap_size": gap.gap_size,
                        }),
                        "system",
                    )
                )
            except Exception as e:
                logger.error(f"Failed to log gap event: {e}")
            
            logger.warning(
                f"WebSocket sequence gap detected: expected {last_seq + 1}, "
                f"got {ws_sequence} (gap of {gap_size} messages)"
            )
        
        self._last_ws_sequence[key] = ws_sequence
    
    def log_fill(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee: float = 0.0,
        trade_id: Optional[str] = None,
        ws_sequence: Optional[int] = None,
    ) -> Optional[int]:
        """
        Log a fill event.
        
        Args:
            order_id: Order ID
            token_id: Token ID
            side: BUY or SELL
            price: Fill price
            size: Fill size
            fee: Fee amount
            trade_id: Optional trade ID
            ws_sequence: Optional WebSocket sequence number
            
        Returns:
            Sequence number assigned to this event
        """
        payload = {
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "fee": fee,
            "trade_id": trade_id,
        }
        return self.log_event(EventType.FILL, payload, "websocket", ws_sequence)
    
    def log_order_update(
        self,
        order_id: str,
        token_id: str,
        side: str,
        status: str,
        original_size: float,
        remaining_size: float,
        ws_sequence: Optional[int] = None,
    ) -> Optional[int]:
        """
        Log an order update event.
        
        Args:
            order_id: Order ID
            token_id: Token ID
            side: BUY or SELL
            status: Order status
            original_size: Original order size
            remaining_size: Remaining order size
            ws_sequence: Optional WebSocket sequence number
            
        Returns:
            Sequence number assigned to this event
        """
        payload = {
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "status": status,
            "original_size": original_size,
            "remaining_size": remaining_size,
        }
        return self.log_event(EventType.ORDER_UPDATE, payload, "websocket", ws_sequence)
    
    def log_reconciliation(
        self,
        open_orders_count: int,
        pending_buys_adjusted: Dict[str, float],
        source: str = "api",
    ) -> Optional[int]:
        """
        Log a reconciliation event.
        
        Args:
            open_orders_count: Number of open orders from API
            pending_buys_adjusted: Dict of token_id -> adjustment amount
            source: Source of reconciliation data
            
        Returns:
            Sequence number assigned to this event
        """
        payload = {
            "open_orders_count": open_orders_count,
            "pending_buys_adjusted": pending_buys_adjusted,
        }
        return self.log_event(EventType.RECONCILIATION, payload, source)
    
    def get_events(
        self,
        since_sequence: Optional[int] = None,
        event_type: Optional[EventType] = None,
        limit: int = 1000,
    ) -> List[LedgerEvent]:
        """
        Get events from the ledger.
        
        Args:
            since_sequence: Only return events after this sequence number
            event_type: Filter by event type
            limit: Maximum number of events to return
            
        Returns:
            List of events
        """
        if not self.is_enabled:
            return []
        
        with self._lock:
            try:
                query = "SELECT sequence_number, event_type, timestamp, payload, source FROM events"
                params = []
                conditions = []
                
                if since_sequence is not None:
                    conditions.append("sequence_number > ?")
                    params.append(since_sequence)
                
                if event_type is not None:
                    conditions.append("event_type = ?")
                    params.append(event_type.value)
                
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                
                query += " ORDER BY sequence_number ASC LIMIT ?"
                params.append(limit)
                
                cursor = self._conn.execute(query, params)
                rows = cursor.fetchall()
                
                events = []
                for row in rows:
                    events.append(LedgerEvent(
                        sequence_number=row[0],
                        event_type=EventType(row[1]),
                        timestamp=datetime.fromisoformat(row[2]),
                        payload=json.loads(row[3]),
                        source=row[4],
                    ))
                
                return events
                
            except Exception as e:
                logger.error(f"Failed to get events: {e}")
                return []
    
    def get_unresolved_gaps(self) -> List[GapInfo]:
        """
        Get list of unresolved gaps.
        
        Returns:
            List of GapInfo for gaps not yet resolved
        """
        return list(self._gaps)
    
    def has_unresolved_gaps(self) -> bool:
        """
        Check if there are any unresolved gaps.
        
        Returns:
            True if there are gaps that haven't been resolved
        """
        return len(self._gaps) > 0
    
    def clear_gaps(self) -> None:
        """Clear the list of detected gaps (after reconciliation)."""
        with self._lock:
            # Mark gaps as resolved in DB
            if self.is_enabled:
                try:
                    self._conn.execute(
                        "UPDATE gaps SET resolved_at = ? WHERE resolved_at IS NULL",
                        (datetime.now(timezone.utc).isoformat(),)
                    )
                except Exception as e:
                    logger.error(f"Failed to mark gaps resolved: {e}")
            
            self._gaps.clear()
            logger.info("Cleared all unresolved gaps after reconciliation")
    
    def get_fills_since(
        self,
        since_sequence: Optional[int] = None,
        token_id: Optional[str] = None,
    ) -> List[LedgerEvent]:
        """
        Get fill events since a sequence number.
        
        Args:
            since_sequence: Only return fills after this sequence
            token_id: Optional filter by token ID
            
        Returns:
            List of fill events
        """
        fills = self.get_events(since_sequence, EventType.FILL)
        
        if token_id:
            fills = [f for f in fills if f.payload.get("token_id") == token_id]
        
        return fills
    
    def get_order_updates_for_order(self, order_id: str) -> List[LedgerEvent]:
        """
        Get all order updates for a specific order.
        
        Args:
            order_id: The order ID to get updates for
            
        Returns:
            List of order update events for this order
        """
        updates = self.get_events(event_type=EventType.ORDER_UPDATE)
        return [u for u in updates if u.payload.get("order_id") == order_id]
    
    def get_event_count(self) -> int:
        """Get total number of events in the ledger."""
        if not self.is_enabled:
            return 0
        
        with self._lock:
            try:
                cursor = self._conn.execute("SELECT COUNT(*) FROM events")
                row = cursor.fetchone()
                return row[0] if row else 0
            except Exception as e:
                logger.error(f"Failed to get event count: {e}")
                return 0
    
    def get_summary(self) -> Dict[str, Any]:
        """
        Get summary statistics about the ledger.
        
        Returns:
            Dict with summary statistics
        """
        if not self.is_enabled:
            return {"enabled": False}
        
        with self._lock:
            try:
                # Get counts by event type
                cursor = self._conn.execute("""
                    SELECT event_type, COUNT(*) 
                    FROM events 
                    GROUP BY event_type
                """)
                counts = {row[0]: row[1] for row in cursor.fetchall()}
                
                # Get gap count
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM gaps WHERE resolved_at IS NULL"
                )
                unresolved_gaps = cursor.fetchone()[0]
                
                return {
                    "enabled": True,
                    "current_sequence": self._sequence_number,
                    "total_events": sum(counts.values()),
                    "events_by_type": counts,
                    "unresolved_gaps": unresolved_gaps,
                    "in_memory_gaps": len(self._gaps),
                }
                
            except Exception as e:
                logger.error(f"Failed to get summary: {e}")
                return {"enabled": True, "error": str(e)}
    
    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            try:
                self._conn.close()
                logger.info("Event ledger closed")
            except Exception as e:
                logger.error(f"Error closing event ledger: {e}")
            finally:
                self._conn = None
