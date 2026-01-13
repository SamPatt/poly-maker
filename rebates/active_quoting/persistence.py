"""
Persistence Layer for Active Quoting Bot.

Provides a clean interface for the bot to persist state to PostgreSQL.
Wraps db.pg_client functions and handles errors gracefully
(bot continues even if DB operations fail).

Key features:
- Position persistence across restarts
- Fill history for analytics
- Markout sample storage
- Session tracking
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

from .models import Position, Fill, OrderSide
from .fill_analytics import FillRecord, MarkoutSample

logger = logging.getLogger(__name__)


# Database availability flag - set to False if DB is unavailable
DB_AVAILABLE = True


def _check_db_available() -> bool:
    """Check if database operations are available."""
    global DB_AVAILABLE
    if not DB_AVAILABLE:
        return False

    try:
        from db.pg_client import DB_ENABLED
        return DB_ENABLED
    except ImportError:
        logger.warning("Database module not available - persistence disabled")
        DB_AVAILABLE = False
        return False


@dataclass
class PersistenceConfig:
    """Configuration for persistence layer."""

    enabled: bool = True
    save_positions: bool = True
    save_fills: bool = True
    save_markouts: bool = True
    save_sessions: bool = True


class ActiveQuotingPersistence:
    """
    Persistence layer for ActiveQuotingBot.

    Provides clean interface to database operations with graceful error handling.
    If database is unavailable, operations silently fail and bot continues.
    """

    def __init__(self, config: Optional[PersistenceConfig] = None):
        """
        Initialize persistence layer.

        Args:
            config: Optional persistence configuration
        """
        self.config = config or PersistenceConfig()
        self._session_id: Optional[str] = None
        self._db_available = _check_db_available()

        if not self._db_available:
            logger.warning("Database not available - persistence disabled")
        elif not self.config.enabled:
            logger.info("Persistence disabled by configuration")

    @property
    def is_enabled(self) -> bool:
        """Check if persistence is enabled and available."""
        return self.config.enabled and self._db_available

    @property
    def session_id(self) -> Optional[str]:
        """Get current session ID."""
        return self._session_id

    # --- Session Management ---

    def start_session(
        self,
        token_ids: List[str],
        config_snapshot: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Start a new session and persist to database.

        Args:
            token_ids: List of token IDs being quoted
            config_snapshot: Optional config snapshot

        Returns:
            Session ID if successful, None otherwise
        """
        if not self.is_enabled or not self.config.save_sessions:
            self._session_id = str(uuid.uuid4())
            return self._session_id

        try:
            from db.pg_client import save_active_quoting_session

            self._session_id = str(uuid.uuid4())
            success = save_active_quoting_session(
                session_id=self._session_id,
                markets=token_ids,
                config_snapshot=config_snapshot,
            )

            if success:
                logger.info(f"Started session {self._session_id}")
            else:
                logger.warning("Failed to save session to database")

            return self._session_id

        except Exception as e:
            logger.error(f"Error starting session: {e}")
            self._session_id = str(uuid.uuid4())
            return self._session_id

    def end_session(
        self,
        status: str = "STOPPED",
        stats: Optional[dict] = None,
    ) -> bool:
        """
        End the current session.

        Args:
            status: Final status (STOPPED, CRASHED)
            stats: Optional final statistics

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_sessions or not self._session_id:
            return True

        try:
            from db.pg_client import update_active_quoting_session

            update_params = {"session_id": self._session_id, "status": status}

            if stats:
                if "total_fills" in stats:
                    update_params["total_fills"] = stats["total_fills"]
                if "total_volume" in stats:
                    update_params["total_volume"] = stats["total_volume"]
                if "total_notional" in stats:
                    update_params["total_notional"] = stats["total_notional"]
                if "net_fees" in stats:
                    update_params["net_fees"] = stats["net_fees"]
                if "realized_pnl" in stats:
                    update_params["realized_pnl"] = stats["realized_pnl"]

            success = update_active_quoting_session(**update_params)

            if success:
                logger.info(f"Ended session {self._session_id} with status {status}")
            else:
                logger.warning("Failed to update session in database")

            return success

        except Exception as e:
            logger.error(f"Error ending session: {e}")
            return False

    def update_session_stats(
        self,
        total_fills: Optional[int] = None,
        total_volume: Optional[float] = None,
        total_notional: Optional[float] = None,
        net_fees: Optional[float] = None,
        realized_pnl: Optional[float] = None,
    ) -> bool:
        """
        Update session statistics.

        Args:
            total_fills: Total fill count
            total_volume: Total volume in shares
            total_notional: Total notional value
            net_fees: Net fees (rebates - fees)
            realized_pnl: Realized P&L

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_sessions or not self._session_id:
            return True

        try:
            from db.pg_client import update_active_quoting_session

            return update_active_quoting_session(
                session_id=self._session_id,
                total_fills=total_fills,
                total_volume=total_volume,
                total_notional=total_notional,
                net_fees=net_fees,
                realized_pnl=realized_pnl,
            )
        except Exception as e:
            logger.error(f"Error updating session stats: {e}")
            return False

    # --- Position Persistence ---

    def save_position(
        self,
        position: Position,
        market_name: Optional[str] = None,
    ) -> bool:
        """
        Save a position to the database.

        Args:
            position: Position object
            market_name: Optional human-readable market name

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_positions:
            return True

        try:
            from db.pg_client import save_active_quoting_position

            return save_active_quoting_position(
                token_id=position.token_id,
                size=position.size,
                avg_price=position.avg_entry_price,
                realized_pnl=position.realized_pnl,
                total_fees=position.total_fees_paid,
                market_name=market_name,
            )
        except Exception as e:
            logger.error(f"Error saving position: {e}")
            return False

    def load_positions(self) -> Dict[str, Position]:
        """
        Load all positions from database.

        Returns:
            Dict mapping token_id -> Position
        """
        if not self.is_enabled or not self.config.save_positions:
            return {}

        try:
            from db.pg_client import get_active_quoting_positions

            df = get_active_quoting_positions()
            if df.empty:
                return {}

            positions = {}
            for _, row in df.iterrows():
                token_id = row["token_id"]
                positions[token_id] = Position(
                    token_id=token_id,
                    size=float(row["size"]),
                    avg_entry_price=float(row["avg_price"]),
                    realized_pnl=float(row.get("realized_pnl", 0.0)),
                    total_fees_paid=float(row.get("total_fees", 0.0)),
                )

            logger.info(f"Loaded {len(positions)} positions from database")
            return positions

        except Exception as e:
            logger.error(f"Error loading positions: {e}")
            return {}

    def clear_position(self, token_id: str) -> bool:
        """
        Clear/delete a position from the database.

        Args:
            token_id: Token ID to clear

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_positions:
            return True

        try:
            from db.pg_client import delete_active_quoting_position

            success = delete_active_quoting_position(token_id=token_id)
            if success:
                logger.debug(f"Cleared position from DB for {token_id[:20]}...")
            return success
        except Exception as e:
            logger.error(f"Error clearing position: {e}")
            return False

    def clear_all_positions(self) -> bool:
        """
        Clear all positions from the database.

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_positions:
            return True

        try:
            from db.pg_client import clear_all_active_quoting_positions

            success = clear_all_active_quoting_positions()
            if success:
                logger.info("Cleared all positions from DB")
            return success
        except Exception as e:
            logger.error(f"Error clearing all positions: {e}")
            return False

    # --- Fill Persistence ---

    def save_fill(
        self,
        fill: Fill,
        mid_at_fill: Optional[float] = None,
        market_name: Optional[str] = None,
    ) -> bool:
        """
        Save a fill to the database.

        Args:
            fill: Fill object
            mid_at_fill: Mid price at time of fill
            market_name: Optional market name

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_fills:
            return True

        try:
            from db.pg_client import save_active_quoting_fill

            fill_id = fill.trade_id or f"{fill.order_id}_{fill.timestamp.timestamp()}"

            return save_active_quoting_fill(
                fill_id=fill_id,
                token_id=fill.token_id,
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
                mid_at_fill=mid_at_fill,
                order_id=fill.order_id,
                trade_id=fill.trade_id,
                market_name=market_name,
                timestamp=fill.timestamp.isoformat() if fill.timestamp else None,
            )
        except Exception as e:
            logger.error(f"Error saving fill: {e}")
            return False

    def save_fill_record(
        self,
        record: FillRecord,
        market_name: Optional[str] = None,
    ) -> bool:
        """
        Save a FillRecord (fill with markout tracking).

        Args:
            record: FillRecord object
            market_name: Optional market name

        Returns:
            True if successful
        """
        if not self.is_enabled:
            return True

        try:
            # Save the fill
            success = self.save_fill(
                fill=record.fill,
                mid_at_fill=record.mid_price_at_fill,
                market_name=market_name,
            )

            if not success:
                return False

            # Save initial markout samples (without captured values)
            if self.config.save_markouts:
                from db.pg_client import save_active_quoting_markout

                for horizon, sample in record.markouts.items():
                    save_active_quoting_markout(
                        fill_id=record.fill_id,
                        horizon_seconds=horizon,
                        mid_at_fill=sample.mid_at_fill,
                    )

            return True

        except Exception as e:
            logger.error(f"Error saving fill record: {e}")
            return False

    # --- Markout Persistence ---

    def save_markout(self, sample: MarkoutSample) -> bool:
        """
        Save a markout sample.

        Args:
            sample: MarkoutSample object

        Returns:
            True if successful
        """
        if not self.is_enabled or not self.config.save_markouts:
            return True

        try:
            from db.pg_client import save_active_quoting_markout

            return save_active_quoting_markout(
                fill_id=sample.fill_id,
                horizon_seconds=sample.horizon_seconds,
                mid_at_fill=sample.mid_at_fill,
                mid_at_horizon=sample.mid_at_horizon,
                markout=sample.markout,
                markout_bps=sample.markout_bps,
            )
        except Exception as e:
            logger.error(f"Error saving markout: {e}")
            return False

    def load_pending_markouts(self) -> List[dict]:
        """
        Load pending markout captures from database.

        Used on startup to recover pending markout captures.

        Returns:
            List of dicts with fill_id, token_id, horizon_seconds, etc.
        """
        if not self.is_enabled or not self.config.save_markouts:
            return []

        try:
            from db.pg_client import get_pending_markout_captures

            df = get_pending_markout_captures()
            if df.empty:
                return []

            pending = df.to_dict("records")
            logger.info(f"Loaded {len(pending)} pending markout captures from database")
            return pending

        except Exception as e:
            logger.error(f"Error loading pending markouts: {e}")
            return []

    # --- Analytics Queries ---

    def get_fill_history(
        self,
        token_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """
        Get fill history from database.

        Args:
            token_id: Optional token filter
            limit: Maximum records to return

        Returns:
            List of fill dicts
        """
        if not self.is_enabled:
            return []

        try:
            from db.pg_client import get_active_quoting_fills

            df = get_active_quoting_fills(token_id=token_id, limit=limit)
            if df.empty:
                return []

            return df.to_dict("records")

        except Exception as e:
            logger.error(f"Error getting fill history: {e}")
            return []

    def get_markout_stats(
        self,
        token_id: Optional[str] = None,
    ) -> Dict[int, float]:
        """
        Get average markout by horizon from database.

        Args:
            token_id: Optional token filter

        Returns:
            Dict mapping horizon_seconds -> avg_markout_bps
        """
        if not self.is_enabled:
            return {}

        try:
            from db.pg_client import get_active_quoting_markout_stats

            df = get_active_quoting_markout_stats(token_id=token_id)
            if df.empty:
                return {}

            return {
                int(row["horizon_seconds"]): float(row.get("avg_markout_bps", 0))
                for _, row in df.iterrows()
            }

        except Exception as e:
            logger.error(f"Error getting markout stats: {e}")
            return {}

    # --- Cleanup ---

    def cleanup_old_data(self, days: int = 30) -> dict:
        """
        Clean up old data from database.

        Args:
            days: Delete data older than this many days

        Returns:
            Dict with counts of deleted records
        """
        if not self.is_enabled:
            return {}

        try:
            from db.pg_client import cleanup_old_active_quoting_data

            deleted = cleanup_old_active_quoting_data(days=days)
            logger.info(f"Cleaned up old data: {deleted}")
            return deleted

        except Exception as e:
            logger.error(f"Error cleaning up data: {e}")
            return {}


# Convenience function to create persistence instance
def create_persistence(enabled: bool = True) -> ActiveQuotingPersistence:
    """
    Create a persistence instance.

    Args:
        enabled: Whether persistence is enabled

    Returns:
        ActiveQuotingPersistence instance
    """
    return ActiveQuotingPersistence(PersistenceConfig(enabled=enabled))


__all__ = [
    "PersistenceConfig",
    "ActiveQuotingPersistence",
    "create_persistence",
]
