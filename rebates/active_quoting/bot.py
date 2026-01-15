"""
ActiveQuotingBot - Main orchestration for active two-sided quoting strategy.

Implements:
- Main ActiveQuotingBot class that wires all components together
- Market discovery using market_finder.py
- Startup sequence: connect WebSockets -> fetch initial state -> start quoting
- Main loop: process orderbook updates -> calculate quotes -> place/update orders
- Graceful shutdown with order cancellation
- Integration with RiskManager for circuit breaker
- Multi-market support with batch order management
- Telegram alerts for key events (Phase 6)
- Database persistence for state recovery (Phase 6)
- WebSocket gap safety halts to prevent position limit violations (Phase 6)
- End-of-market wind-down strategy (stop buys, sell excess at profit)
"""
import asyncio
import logging
import os
import signal
import sys
import time
import requests
import aiohttp
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Tuple

from .config import ActiveQuotingConfig
from .models import (
    OrderbookState,
    Quote,
    Fill,
    MarketState,
    Position,
    OrderSide,
    OrderStatus,
    MomentumState,
)
from .orderbook_manager import OrderbookManager
from .user_channel_manager import UserChannelManager
from .quote_engine import QuoteEngine, QuoteAction
from .order_manager import OrderManager
from .inventory_manager import InventoryManager
from .momentum_detector import MomentumDetector
from .risk_manager import RiskManager, CircuitBreakerState, HaltReason
from .fill_analytics import FillAnalytics
from .persistence import ActiveQuotingPersistence, PersistenceConfig
from .event_ledger import EventLedger, EventType
from .pnl_tracker import PnLTracker
from .redemption_manager import RedemptionManager
from .alerts import (
    TELEGRAM_ENABLED,
    send_active_quoting_startup_alert,
    send_active_quoting_shutdown_alert,
    send_active_quoting_fill_alert,
    send_active_quoting_circuit_breaker_alert,
    send_active_quoting_daily_summary,
    send_active_quoting_market_halt_alert,
    send_active_quoting_redemption_alert,
    send_active_quoting_market_resolution_summary,
    TelegramCommandHandler,
)

logger = logging.getLogger(__name__)


class WindDownPhase(Enum):
    """Phases of end-of-market wind-down."""
    NORMAL = "NORMAL"  # Normal quoting
    WIND_DOWN = "WIND_DOWN"  # No buys, maker sells only (5min to 40sec)
    TAKER_EXIT = "TAKER_EXIT"  # Taker sell excess if price < threshold (40sec to 0)
    MARKET_ENDED = "MARKET_ENDED"  # Market has ended, no trading


@dataclass
class WindDownState:
    """State of wind-down for a market."""
    phase: WindDownPhase
    seconds_remaining: float
    excess_token_id: Optional[str] = None  # Token with excess position to sell
    excess_size: float = 0.0  # Size of excess position
    avg_entry_price: float = 0.0  # Entry price for profitable sell calculation
    paired_token_id: Optional[str] = None  # The other token in the pair
    current_price: Optional[float] = None  # Current best bid for excess token


class ActiveQuotingBot:
    """
    Main orchestration class for active two-sided quoting.

    This bot:
    1. Discovers and tracks 15-minute crypto markets
    2. Maintains real-time orderbook state via WebSocket
    3. Calculates optimal quotes with inventory skew
    4. Places/updates orders with proper fee handling
    5. Tracks fills and calculates markout metrics
    6. Implements circuit breaker for risk management
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        poly_client: Any = None,
        enable_persistence: bool = True,
        enable_alerts: bool = True,
    ):
        """
        Initialize the ActiveQuotingBot.

        Args:
            config: Active quoting configuration
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            api_passphrase: Polymarket API passphrase
            poly_client: Optional PolymarketClient for order placement
            enable_persistence: Enable database persistence (default: True)
            enable_alerts: Enable Telegram alerts (default: True)
        """
        self.config = config
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._poly_client = poly_client

        # Feature flags
        self._enable_alerts = enable_alerts and TELEGRAM_ENABLED
        self._enable_persistence = enable_persistence

        # Initialize components
        self._init_components()

        # State
        self._running: bool = False  # True when actively trading
        self._stopped: bool = False  # True when fully stopped (process should exit)
        self._markets: Dict[str, MarketState] = {}  # token_id -> MarketState
        self._active_tokens: Set[str] = set()
        self._last_quote_refresh: Dict[str, datetime] = {}
        self._global_refresh_count: int = 0
        self._global_refresh_window_start: float = 0.0
        self._market_names: Dict[str, str] = {}  # token_id -> market name for alerts
        self._market_end_times: Dict[str, datetime] = {}  # token_id -> end_time for resolution summaries
        self._markets_summarized: Set[str] = set()  # Markets that have already had summaries sent
        self._start_time: Optional[datetime] = None

        # Wind-down state tracking
        self._wind_down_taker_executed: Set[str] = set()  # condition_ids where taker exit was executed
        self._wind_down_logged: Dict[str, float] = {}  # token_id -> last log time (avoid spam)
        self._wind_down_orders_cancelled: Set[str] = set()  # condition_ids where orders were cancelled on wind-down entry

        # Tasks
        self._main_task: Optional[asyncio.Task] = None
        self._markout_task: Optional[asyncio.Task] = None
        self._daily_summary_task: Optional[asyncio.Task] = None
        self._market_ws_task: Optional[asyncio.Task] = None
        self._user_ws_task: Optional[asyncio.Task] = None

        # Telegram command handler for bot control commands
        self._telegram_handler: Optional[TelegramCommandHandler] = None

        # Position sync tracking (fallback for WebSocket fill issues)
        self._last_position_sync: float = 0.0
        self._position_sync_interval: float = 5.0  # Sync positions every 5 seconds

        # Order reconciliation tracking (to catch missed WebSocket messages)
        self._last_reconcile_time: float = 0.0
        self._reconcile_interval: float = 60.0  # Reconcile orders every 60 seconds

        # WebSocket gap safety halt tracking (Phase 6)
        self._gap_reconcile_attempts: int = 0  # Counter for failed reconciliation attempts
        self._last_gap_recovery_time: float = 0.0  # Last recovery attempt when halted due to gaps

    def _init_components(self) -> None:
        """Initialize all sub-components."""
        # Persistence layer (Phase 6)
        self.persistence = ActiveQuotingPersistence(
            PersistenceConfig(enabled=self._enable_persistence)
        )

        # Event ledger for gap detection and audit trail (Phase 5)
        # Use environment variable or default to local data directory
        event_ledger_path = os.getenv("EVENT_LEDGER_PATH")
        if event_ledger_path is None and self._enable_persistence:
            # Default to data directory relative to project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            data_dir = os.path.join(project_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            event_ledger_path = os.path.join(data_dir, "event_ledger.db")
        self.event_ledger = EventLedger(
            db_path=event_ledger_path,
            enabled=self._enable_persistence,
        )
        # Core managers
        self.inventory_manager = InventoryManager(self.config)

        self.quote_engine = QuoteEngine(
            config=self.config,
            inventory_manager=self.inventory_manager,
        )

        self.momentum_detector = MomentumDetector(
            config=self.config,
            on_momentum=self._on_momentum_detected,
        )

        self.risk_manager = RiskManager(
            config=self.config,
            on_state_change=self._on_circuit_breaker_state_change,
            on_market_halt=self._on_market_halt,
            on_kill_switch=self._on_kill_switch,
        )

        self.order_manager = OrderManager(
            config=self.config,
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
            poly_client=self._poly_client,
        )

        self.fill_analytics = FillAnalytics(
            on_markout_captured=self._on_markout_captured,
        )

        # PnL tracker for real-time profit/loss visibility
        self.pnl_tracker = PnLTracker(
            log_interval_seconds=60,  # Log summary every 60 seconds
            recent_trades_limit=100,
        )

        self.redemption_manager = RedemptionManager(
            on_redemption_complete=self._on_redemption_complete,
            on_redemption_error=self._on_redemption_error,
            resolution_check_delay_seconds=60.0,  # Wait 60s after market end before first check
            resolution_check_interval_seconds=30.0,  # Check every 30s until resolved
            max_resolution_check_attempts=20,  # Give up after ~10 minutes
        )

        # WebSocket managers
        self.orderbook_manager = OrderbookManager(
            config=self.config,
            on_book_update=self._on_book_update,
            on_trade=self._on_trade,
            on_tick_size_change=self._on_tick_size_change,
            on_disconnect=self._on_market_ws_disconnect,
        )

        # Get wallet address from poly_client for fill verification
        wallet_address = None
        if self._poly_client:
            wallet_address = getattr(self._poly_client, 'browser_wallet', None)

        self.user_channel_manager = UserChannelManager(
            config=self.config,
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
            wallet_address=wallet_address,
            on_fill=self._on_fill,
            on_order_update=self._on_order_update,
            on_disconnect=self._on_user_ws_disconnect,
        )

    # --- Position Syncing ---

    async def _sync_positions_from_api(self, token_ids: Set[str]) -> int:
        """
        Sync positions from Polymarket API for the given tokens (async).

        This fetches actual positions from the exchange and updates
        the inventory manager to match reality.

        IMPORTANT: Uses async HTTP to avoid blocking the event loop.

        Args:
            token_ids: Set of token IDs to sync positions for

        Returns:
            Number of positions synced
        """
        if not self._poly_client:
            logger.warning("No poly_client available, cannot sync positions from API")
            return 0

        try:
            # Get wallet address from poly_client
            wallet_address = self._poly_client.browser_wallet

            # Fetch positions from Polymarket data API using async HTTP
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://data-api.polymarket.com/positions?user={wallet_address}",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        logger.error(f"API returned status {response.status}")
                        return 0
                    positions_data = await response.json()

            # Build a dict of token_id -> (size, avg_price) from API
            api_positions = {}
            logger.info(f"API returned {len(positions_data)} total positions")
            for pos in positions_data:
                token_id = str(pos.get("asset", ""))
                size = float(pos.get("size", 0))
                avg_price = float(pos.get("avgPrice", 0.5))
                title = pos.get("title", "")
                if token_id in token_ids:
                    api_positions[token_id] = (size, avg_price)
                    logger.info(f"API position match: {token_id[:20]}... size={size:.2f} title={title[:40]}")

            logger.info(f"Matched {len(api_positions)} positions for {len(token_ids)} active tokens")

            synced_count = 0
            for token_id in token_ids:
                old_position = self.inventory_manager.get_position(token_id)
                old_size = old_position.size

                # Get position from API, default to 0 if not found
                if token_id in api_positions:
                    size, avg_price = api_positions[token_id]
                else:
                    size = 0.0
                    avg_price = 0.5

                # Only update if position changed
                if abs(size - old_size) >= 0.01:
                    pending_buys = self.inventory_manager.get_pending_buy_size(token_id)
                    discrepancy = old_size - size

                    # Log discrepancies for monitoring
                    if abs(discrepancy) > 1.0:
                        logger.info(
                            f"Position sync for {token_id[:20]}...: "
                            f"API={size:.2f}, internal={old_size:.2f}, "
                            f"discrepancy={discrepancy:+.2f}, pending_orders={pending_buys:.0f}"
                        )

                    # API is source of truth - always sync position
                    # No blocking for pending_buys - that caused deadlock where
                    # pending wasn't cleared because sync was blocked
                    self.inventory_manager.set_position(token_id, size, avg_price)

                    # Clear pending buys since API position already reflects fills
                    # This prevents double-counting filled orders as both position AND pending
                    self.inventory_manager.clear_pending_buys(token_id)

                    # Persist to DB to prevent stale positions on restart
                    if self.persistence.is_enabled:
                        position = self.inventory_manager.get_position(token_id)
                        if size > 0:
                            self.persistence.save_position(position)
                        else:
                            # Clear position from DB if it is now 0
                            self.persistence.clear_position(token_id)

                    # Log significant changes
                    if abs(size - old_size) >= 1.0:
                        logger.info(
                            f"Position synced from API for {token_id[:20]}...: "
                            f"{old_size:.0f} -> {size:.0f} shares"
                        )
                    synced_count += 1

            if synced_count > 0:
                logger.debug(f"Synced {synced_count} position updates from Polymarket API")
            return synced_count

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch positions from API: {e}")
            return 0
        except Exception as e:
            logger.error(f"Error syncing positions from API: {e}")
            return 0


    async def _reconcile_orders(self) -> None:
        """
        Reconcile internal order state with orders fetched from REST API.

        This catches any missed WebSocket messages by:
        1. Fetching open orders from the CLOB API
        2. Calling user_channel_manager.reconcile_with_api_orders()
        3. Reconciling pending buy reservations based on actual open orders
        4. Checking for unresolved WebSocket gaps and triggering safety halt if needed (Phase 6)

        Should be called:
        - On startup after WebSocket connects
        - Periodically (every 60 seconds) in the main loop
        - Immediately when WebSocket gaps are detected
        """
        if not self._poly_client:
            logger.warning("No poly_client available, cannot reconcile orders")
            return

        try:
            # Fetch open orders from CLOB API
            open_orders = self._poly_client.client.get_orders()

            if open_orders:
                logger.debug(f"Reconciling with {len(open_orders)} open orders from API")
            else:
                logger.debug("No open orders from API to reconcile")

            # Reconcile user channel manager state with API orders
            self.user_channel_manager.reconcile_with_api_orders(open_orders)
            # Reconcile order manager state for cancel_all accuracy
            self.order_manager.reconcile_with_api_orders(open_orders)

            # Reconcile pending buy reservations
            # Build set of token_ids with open BUY orders and their sizes
            api_buy_sizes: Dict[str, float] = {}
            for order in open_orders:
                side = order.get("side", "").upper()
                if side == "BUY":
                    token_id = order.get("asset_id") or order.get("token_id", "")
                    remaining = float(order.get("size_matched", 0))
                    original = float(order.get("original_size", order.get("size", 0)))
                    order_remaining = original - remaining
                    if token_id:
                        api_buy_sizes[token_id] = api_buy_sizes.get(token_id, 0) + order_remaining

            # For each active token, reconcile pending buys
            for token_id in self._active_tokens:
                expected_pending = api_buy_sizes.get(token_id, 0.0)
                current_pending = self.inventory_manager.get_pending_buy_size(token_id)

                # If we think we have more pending than API shows, correct it
                if current_pending > expected_pending + 0.01:
                    excess = current_pending - expected_pending
                    logger.warning(
                        f"Reconcile: Reducing pending buys for {token_id[:20]}... "
                        f"from {current_pending:.2f} to {expected_pending:.2f} (excess: {excess:.2f})"
                    )
                    self.inventory_manager.release_pending_buy(token_id, excess)
                elif expected_pending > current_pending + 0.01:
                    delta = expected_pending - current_pending
                    logger.warning(
                        f"Reconcile: Increasing pending buys for {token_id[:20]}... "
                        f"from {current_pending:.2f} to {expected_pending:.2f} (delta: {delta:.2f})"
                    )
                    self.inventory_manager.reserve_pending_buy(token_id, delta)

            logger.debug("Order reconciliation complete")

            # Log reconciliation to event ledger (Phase 5)
            pending_buys_adjusted = {}
            for token_id in self._active_tokens:
                expected = api_buy_sizes.get(token_id, 0.0)
                current = self.inventory_manager.get_pending_buy_size(token_id)
                if abs(current - expected) > 0.01:
                    pending_buys_adjusted[token_id] = expected - current
            self.event_ledger.log_reconciliation(
                open_orders_count=len(open_orders) if open_orders else 0,
                pending_buys_adjusted=pending_buys_adjusted,
                source="api",
            )

            # Phase 6: Check if gaps persist after reconciliation
            if self.event_ledger.has_unresolved_gaps():
                gaps = self.event_ledger.get_unresolved_gaps()
                self._gap_reconcile_attempts += 1
                
                # Check if we should trigger safety halt
                if self.config.halt_on_ws_gaps:
                    if self._gap_reconcile_attempts >= self.config.ws_gap_reconcile_attempts:
                        # Gaps persist after max attempts - trigger safety halt
                        gap_details = ", ".join(
                            f"seq {g.expected_start}-{g.expected_end}"
                            for g in gaps[:3]  # Show first 3 gaps
                        )
                        logger.error(
                            f"WebSocket gaps persist after {self._gap_reconcile_attempts} "
                            f"reconciliation attempts - triggering safety halt. "
                            f"Gaps: {gap_details}"
                        )
                        await self.risk_manager.trigger_halt(
                            f"WebSocket message gaps unresolved after {self._gap_reconcile_attempts} attempts",
                            HaltReason.WS_GAP_UNRESOLVED,
                        )
                        # Reset attempt counter after halt
                        self._gap_reconcile_attempts = 0
                    else:
                        logger.warning(
                            f"WebSocket gaps persist after reconciliation "
                            f"(attempt {self._gap_reconcile_attempts}/{self.config.ws_gap_reconcile_attempts})"
                        )
                else:
                    # Safety halt disabled - just clear gaps and warn
                    logger.warning(
                        f"WebSocket gaps detected but halt_on_ws_gaps=False, clearing gaps"
                    )
                    self.event_ledger.clear_gaps()
                    self._gap_reconcile_attempts = 0
            else:
                # Gaps resolved - reset attempt counter
                if self._gap_reconcile_attempts > 0:
                    logger.info(
                        f"WebSocket gaps resolved after {self._gap_reconcile_attempts} attempt(s)"
                    )
                self._gap_reconcile_attempts = 0
                
                # If we were halted due to WS gaps and gaps are now resolved, recover
                if self.risk_manager.is_halted_due_to_ws_gaps():
                    logger.info("Gaps resolved - initiating recovery from WS gap halt")
                    await self.risk_manager.recover_from_ws_gap_halt()

        except Exception as e:
            logger.error(f"Error reconciling orders: {e}")

    # --- Startup/Shutdown ---

    async def start(
        self,
        token_ids: List[str],
        market_names: Optional[Dict[str, str]] = None,
        market_times: Optional[Dict[str, tuple[datetime, datetime]]] = None,
        condition_ids: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Start the bot for the given token IDs.

        Args:
            token_ids: List of token IDs to quote on
            market_names: Optional mapping of token_id -> human-readable name
            market_times: Optional mapping of token_id -> (start_time, end_time) in UTC
            condition_ids: Optional mapping of token_id -> condition_id for redemption
        """
        if self._running:
            logger.warning("Bot is already running")
            return

        if not token_ids:
            logger.error("No token IDs provided")
            return

        logger.info(f"Starting ActiveQuotingBot for {len(token_ids)} tokens")
        self._running = True
        self._active_tokens = set(token_ids)
        self._start_time = datetime.now(timezone.utc)

        # Store market names for alerts and P&L tracking
        if market_names:
            self._market_names = market_names.copy()
            # Also set market names in PnL tracker for readable trade logs
            for token_id, name in market_names.items():
                self.pnl_tracker.set_market_name(token_id, name)

        # Store market end times for resolution summaries
        if market_times:
            for token_id, (start_time, end_time) in market_times.items():
                self._market_end_times[token_id] = end_time

        # Sync positions from Polymarket API (authoritative source)
        # This ensures we know about positions from previous sessions or manual trades
        await self._sync_positions_from_api(self._active_tokens)

        # Load from database for additional state (realized PnL, fees)
        # NOTE: API is authoritative for position SIZE - DB is only for metadata
        if self.persistence.is_enabled:
            saved_positions = self.persistence.load_positions()
            for token_id, position in saved_positions.items():
                if token_id in self._active_tokens:
                    current = self.inventory_manager.get_position(token_id)

                    # Only restore realized PnL and fees from DB, NOT position size
                    # Position size from API is authoritative
                    if position.realized_pnl != 0:
                        current.realized_pnl = position.realized_pnl
                    if position.total_fees_paid != 0:
                        current.total_fees_paid = position.total_fees_paid

                    # If DB has a position but API says 0, clear the stale DB entry
                    if position.size > 0 and current.size == 0:
                        logger.warning(
                            f"Clearing stale DB position for {token_id[:20]}...: "
                            f"DB had {position.size:.2f} but API says 0"
                        )
                        self.persistence.clear_position(token_id)

            # Recover pending markout captures
            pending = self.persistence.load_pending_markouts()
            if pending:
                logger.info(f"Recovered {len(pending)} pending markout captures")
                # Note: actual recovery would require re-scheduling - simplified here

        # Initialize market states
        for token_id in token_ids:
            # Use restored position if available
            position = self.inventory_manager.get_position(token_id)
            condition_id = condition_ids.get(token_id, "") if condition_ids else ""
            self._markets[token_id] = MarketState(
                token_id=token_id,
                reverse_token_id="",  # Will be set if available
                asset="",
                orderbook=OrderbookState(token_id=token_id),
                momentum=MomentumState(token_id=token_id),
                position=position,
                condition_id=condition_id,
            )

            # Register market time windows for smart stale detection
            if market_times and token_id in market_times:
                start_time, end_time = market_times[token_id]
                self.risk_manager.set_market_time_window(token_id, start_time, end_time)

            # Register token pair for combined drawdown calculation
            # (both UP and DOWN tokens in a binary market share the same condition_id)
            if condition_id:
                self.risk_manager.register_token_pair(token_id, condition_id)

                # Register market for redemption if we have condition_id
                if market_times and token_id in market_times:
                    start_time, end_time = market_times[token_id]
                    self.redemption_manager.register_market(
                        token_id=token_id,
                        condition_id=condition_id,
                        market_end_time=end_time,
                        position_size=position.size,
                    )

        try:
            # Start session in database (Phase 6)
            config_snapshot = {
                "order_size": self.config.order_size_usdc,
                "max_position": self.config.max_position_per_market,
                "dry_run": self.config.dry_run,
            }
            self.persistence.start_session(token_ids, config_snapshot)

            # Start WebSocket connections
            await self._connect_websockets(token_ids)

            # Start main loop and markout processing
            self._main_task = asyncio.create_task(self._main_loop())
            self._markout_task = asyncio.create_task(self._markout_loop())
            self._daily_summary_task = asyncio.create_task(self._daily_summary_loop())

            # Start Telegram command handler for /stopaq, /startaq, /status, and trading bot commands
            if self._enable_alerts:
                self._telegram_handler = TelegramCommandHandler(
                    on_stop_command=lambda: self.pause("Telegram /stopaq command"),
                    on_start_command=self.resume,
                    on_status_command=self._send_status_via_telegram,
                )
                await self._telegram_handler.start()
                logger.info("Telegram command handler started (supports /stopaq, /startaq, /status, /starttrading, /stoptrading, /startgab, /stopgab)")

            logger.info("Bot started successfully")

            # Send startup alert (Phase 6)
            if self._enable_alerts:
                send_active_quoting_startup_alert(
                    market_count=len(token_ids),
                    dry_run=self.config.dry_run,
                    config_summary={
                        "order_size": self.config.order_size_usdc,
                        "max_position": self.config.max_position_per_market,
                    },
                )

        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            await self.stop()
            raise

    async def _connect_websockets(self, token_ids: List[str]) -> None:
        """Connect to WebSocket channels."""
        # Start both WebSocket connections concurrently
        # These tasks run forever (they contain the message loops)
        self._market_ws_task = asyncio.create_task(
            self.orderbook_manager.connect(token_ids)
        )
        self._user_ws_task = asyncio.create_task(
            self.user_channel_manager.connect()
        )

        # Wait briefly for connections to establish
        # Use asyncio.wait() instead of wait_for() because wait_for CANCELS
        # the tasks on timeout, which would close our WebSocket connections
        done, pending = await asyncio.wait(
            [self._market_ws_task, self._user_ws_task],
            timeout=5.0,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Check if any failed immediately
        for task in done:
            try:
                exc = task.exception()
                if exc:
                    logger.error(f"WebSocket task failed on startup: {exc}")
            except asyncio.CancelledError:
                pass

        # Give connections a moment to fully establish
        await asyncio.sleep(1.0)

        # Log connection status
        if self.orderbook_manager.is_connected():
            logger.info("Market WebSocket connected and ready")
        else:
            logger.warning("Market WebSocket not yet reporting as connected")

        if self.user_channel_manager.is_connected():
            logger.info("User WebSocket connected and ready")
        else:
            logger.warning("User WebSocket not yet reporting as connected")

        # Initial order reconciliation to catch any state from previous session
        await self._reconcile_orders()

    async def stop(self, reason: str = "Normal shutdown") -> None:
        """
        Stop the bot gracefully.

        Args:
            reason: Reason for shutdown (for logging/alerts)
        """
        if self._stopped:
            return

        logger.info(f"Stopping ActiveQuotingBot... (reason: {reason})")
        self._running = False
        self._stopped = True

        # Collect final stats for alerts and persistence
        stats = self.fill_analytics.get_summary()

        # Cancel all orders and clear pending reservations
        try:
            self.inventory_manager.clear_all_pending_buys()
            cancelled = await self.order_manager.cancel_all()
            logger.info(f"Cancelled {cancelled} orders on shutdown")
        except Exception as e:
            logger.error(f"Error cancelling orders on shutdown: {e}")

        # Cancel tasks
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        if self._markout_task:
            self._markout_task.cancel()
            try:
                await self._markout_task
            except asyncio.CancelledError:
                pass

        if self._daily_summary_task:
            self._daily_summary_task.cancel()
            try:
                await self._daily_summary_task
            except asyncio.CancelledError:
                pass

        # Stop Telegram command handler
        if self._telegram_handler:
            await self._telegram_handler.stop()
            self._telegram_handler = None

        # Cancel WebSocket tasks (disconnect will close the connections)
        for ws_task in [self._market_ws_task, self._user_ws_task]:
            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass

        # Disconnect WebSockets
        await self.orderbook_manager.disconnect()
        await self.user_channel_manager.disconnect()

        # Close HTTP session
        await self.order_manager.close()

        # Shutdown analytics
        await self.fill_analytics.shutdown()

        # Close event ledger (Phase 5)
        self.event_ledger.close()

        # End session in database (Phase 6)
        status = "CRASHED" if "error" in reason.lower() else "STOPPED"
        self.persistence.end_session(
            status=status,
            stats={
                "total_fills": stats.get("total_fills", 0),
                "total_volume": stats.get("total_volume", 0.0),
                "total_notional": stats.get("total_notional", 0.0),
                "net_fees": stats.get("net_fees", 0.0),
                "realized_pnl": stats.get("realized_pnl", 0.0),
            },
        )

        # Send shutdown alert (Phase 6)
        if self._enable_alerts:
            send_active_quoting_shutdown_alert(
                reason=reason,
                stats={
                    "total_fills": stats.get("total_fills", 0),
                    "net_fees": stats.get("net_fees", 0.0),
                    "realized_pnl": stats.get("realized_pnl", 0.0),
                },
            )

        # Log final P&L summary
        self.pnl_tracker.maybe_log_summary(force=True)

        logger.info("Bot stopped")

    async def pause(self, reason: str = "Telegram /stopaq command") -> None:
        """
        Pause trading without full shutdown.

        Unlike stop(), this keeps the Telegram handler and WebSockets alive
        so that /startaq can resume trading.

        Args:
            reason: Reason for pausing (for logging/alerts)
        """
        if not self._running:
            logger.info("Bot already paused")
            return

        logger.info(f"Pausing ActiveQuotingBot... (reason: {reason})")
        self._running = False

        # Cancel all orders
        try:
            self.inventory_manager.clear_all_pending_buys()
            cancelled = await self.order_manager.cancel_all()
            logger.info(f"Cancelled {cancelled} orders on pause")
        except Exception as e:
            logger.error(f"Error cancelling orders on pause: {e}")

        # Cancel main loop tasks (but keep telegram handler and websockets alive)
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        if self._markout_task and not self._markout_task.done():
            self._markout_task.cancel()
            try:
                await self._markout_task
            except asyncio.CancelledError:
                pass

        if self._daily_summary_task and not self._daily_summary_task.done():
            self._daily_summary_task.cancel()
            try:
                await self._daily_summary_task
            except asyncio.CancelledError:
                pass

        # Send pause alert
        if self._enable_alerts:
            stats = self.fill_analytics.get_summary()
            send_telegram_alert(
                f"AQ Bot PAUSED\n"
                f"Reason: {reason}\n"
                f"Fills: {stats.get('total_fills', 0)}, "
                f"PnL: ${stats.get('realized_pnl', 0.0):.2f}\n"
                f"Use /startaq to resume",
                is_error=False,
            )

        logger.info("Bot paused (use /startaq to resume)")

    async def resume(self) -> None:
        """
        Resume trading after pause.

        Restarts the main loop and markout tasks.
        """
        if self._running:
            logger.info("Bot already running")
            if self._enable_alerts:
                send_telegram_alert("AQ Bot is already running", is_error=False)
            return

        logger.info("Resuming ActiveQuotingBot...")
        self._running = True

        # Restart main loop tasks
        self._main_task = asyncio.create_task(self._main_loop())
        self._markout_task = asyncio.create_task(self._markout_loop())
        self._daily_summary_task = asyncio.create_task(self._daily_summary_loop())

        # Send resume alert
        if self._enable_alerts:
            send_telegram_alert(
                f"AQ Bot RESUMED\n"
                f"Trading restarted",
                is_error=False,
            )

        logger.info("Bot resumed")

    async def run(self, token_ids: List[str]) -> None:
        """
        Run the bot until stopped.

        Args:
            token_ids: List of token IDs to quote on
        """
        await self.start(token_ids)

        # Set up signal handlers (SIGHUP for screen -X quit, SIGINT/SIGTERM for manual)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Wait until fully stopped (not just paused)
        # _running=False means paused, _stopped=True means exit
        while not self._stopped:
            await asyncio.sleep(1)

    # --- Main Loop ---

    async def _main_loop(self) -> None:
        """Main quoting loop."""
        logger.info("Starting main loop")

        while self._running:
            try:
                # Periodic position sync from REST API (fallback for WebSocket fill issues)
                # This ensures we have accurate position data even if fills aren't received
                now = time.time()
                if now - self._last_position_sync >= self._position_sync_interval:
                    await self._sync_positions_from_api(self._active_tokens)
                    self._last_position_sync = now

                # Periodic order reconciliation to catch missed WebSocket messages
                if now - self._last_reconcile_time >= self._reconcile_interval:
                    await self._reconcile_orders()
                    # Check for WebSocket gaps and trigger immediate reconciliation if found
                elif self.event_ledger.has_unresolved_gaps():
                    logger.warning(
                        f"Detected {len(self.event_ledger.get_unresolved_gaps())} "
                        f"unresolved gaps - triggering immediate reconciliation"
                    )
                    await self._reconcile_orders()
                    self._last_reconcile_time = now

                # Check stale feeds
                stale = self.risk_manager.check_stale_feeds()
                if stale:
                    logger.warning(f"Stale feeds detected: {len(stale)} markets")

                # Check for resolved markets that need redemption
                await self._check_redemptions()

                # Check for ended markets and send resolution summaries
                await self._check_market_resolutions()

                # Check circuit breaker state
                if self.risk_manager.state == CircuitBreakerState.HALTED:
                    # Phase 6: If halted due to WS gaps, periodically attempt recovery
                    if self.risk_manager.is_halted_due_to_ws_gaps():
                        if now - self._last_gap_recovery_time >= self.config.ws_gap_recovery_interval_seconds:
                            logger.info("Attempting recovery from WS gap halt via reconciliation")
                            self._last_gap_recovery_time = now
                            await self._reconcile_orders()
                            # If recovery succeeded, don't skip the rest of the loop
                            if self.risk_manager.state != CircuitBreakerState.HALTED:
                                continue
                    await asyncio.sleep(1)
                    continue

                # Check if recovering
                if self.risk_manager.state == CircuitBreakerState.RECOVERING:
                    await self.risk_manager.check_recovery_complete()

                # Process each market
                for token_id in self._active_tokens:
                    await self._process_market(token_id)

                # Periodic P&L summary logging
                self.pnl_tracker.maybe_log_summary()

                # Small delay to prevent tight loop
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                self.risk_manager.record_error()
                await asyncio.sleep(1)

    async def _process_market(self, token_id: str) -> None:
        """Process a single market - calculate and update quotes."""
        # Check wind-down state first
        wind_down = self._get_wind_down_state(token_id)
        if wind_down.phase != WindDownPhase.NORMAL:
            if wind_down.phase == WindDownPhase.MARKET_ENDED:
                # Market has ended, skip processing
                return
            # Handle wind-down (returns True if it handled the market)
            if await self._process_wind_down(token_id, wind_down):
                return

        # Check if we can place orders for this market
        can_place, reason = self.risk_manager.can_place_orders_for_market(token_id)
        if not can_place:
            logger.debug(f"Cannot place orders for {token_id[:20]}...: {reason}")
            return

        # Get market state
        market = self._markets.get(token_id)
        if not market:
            return

        # Get orderbook
        orderbook = self.orderbook_manager.get_orderbook(token_id)
        if not orderbook or not orderbook.is_valid():
            return

        # Update market state with latest orderbook
        market.orderbook = orderbook

        # Get momentum state
        momentum = self.momentum_detector.get_state(token_id)

        # Check cooldown expiry
        self.momentum_detector.check_cooldown_expired(token_id)

        # Calculate quote
        decision = self.quote_engine.calculate_quote_with_manager(
            orderbook=orderbook,
            momentum_state=momentum,
            current_quote=market.last_quote,
        )

        # Debug log for quote decisions (temporary)
        if decision.action == QuoteAction.PLACE_QUOTE:
            logger.info(f"Quote decision for {token_id[:20]}...: {decision.action.value} - {decision.reason}")
        elif decision.action == QuoteAction.CANCEL_ALL:
            logger.debug(f"Quote decision for {token_id[:20]}...: {decision.action.value} - {decision.reason}")

        # Handle quote decision
        if decision.action == QuoteAction.CANCEL_ALL:
            if market.is_quoting:
                await self._cancel_market_quotes(token_id)
                market.is_quoting = False
        elif decision.action == QuoteAction.PLACE_QUOTE:
            await self._place_or_update_quote(token_id, decision.quote)
        # KEEP_CURRENT - do nothing

    async def _place_or_update_quote(self, token_id: str, quote: Quote) -> None:
        """Place or update quote for a market."""
        # Check rate limiting
        if not self._check_refresh_rate(token_id):
            return

        # Get adjusted order sizes based on circuit breaker state
        base_size = self.config.order_size_usdc

        # Get inventory-adjusted sizes
        buy_size, sell_size = self.quote_engine.get_inventory_adjusted_sizes(
            token_id, base_size
        )

        # Apply circuit breaker multiplier
        multiplier = self.risk_manager.get_position_limit_multiplier()
        buy_size *= multiplier
        sell_size *= multiplier

        # Check if we have meaningful sizes
        min_size = 5.0  # Polymarket minimum is 5 shares
        if buy_size < min_size and sell_size < min_size:
            return

        # Adjust quote with new sizes
        adjusted_quote = Quote(
            token_id=quote.token_id,
            bid_price=quote.bid_price,
            ask_price=quote.ask_price,
            bid_size=max(buy_size, min_size) if buy_size >= min_size else 0,
            ask_size=max(sell_size, min_size) if sell_size >= min_size else 0,
            timestamp=quote.timestamp,
        )

        market = self._markets.get(token_id)
        if market and market.last_quote:
            # Cancel existing orders - pending buy reservations will be released
            # when _on_order_update receives CANCELLED confirmation from exchange
            await self.order_manager.cancel_all_for_token(token_id)
            # Wait for exchange to process cancellation
            await asyncio.sleep(0.2)

        # Place orders
        orders_to_place = []
        if adjusted_quote.bid_size >= min_size:
            orders_to_place.append((
                token_id,
                OrderSide.BUY,
                adjusted_quote.bid_price,
                adjusted_quote.bid_size,
                False,  # neg_risk
            ))
        if adjusted_quote.ask_size >= min_size:
            orders_to_place.append((
                token_id,
                OrderSide.SELL,
                adjusted_quote.ask_price,
                adjusted_quote.ask_size,
                False,
            ))

        if orders_to_place:
            result = await self.order_manager.place_orders_batch(orders_to_place)
            if result.all_succeeded:
                # Register placed orders with user channel manager for fill verification
                for order_result in result.successful_orders:
                    if order_result.order_id:
                        self.user_channel_manager.register_placed_order(order_result.order_id)

                # Reserve capacity for buy orders to prevent race condition
                for order in orders_to_place:
                    _, side, _, size, _ = order
                    if side == OrderSide.BUY:
                        self.inventory_manager.reserve_pending_buy(token_id, size)
                if market:
                    market.last_quote = adjusted_quote
                    market.is_quoting = True
                self._last_quote_refresh[token_id] = datetime.utcnow()
                self.risk_manager.clear_errors()
            else:
                # Check if failures are "soft" errors (state issues, not bot failures)
                # Don't count these toward circuit breaker
                soft_errors = {"not enough balance", "order crosses book", "allowance"}
                has_hard_error = False
                for failed in result.failed_orders:
                    error_lower = failed.error_msg.lower()
                    if not any(soft in error_lower for soft in soft_errors):
                        has_hard_error = True
                        break
                if has_hard_error:
                    self.risk_manager.record_error()

    async def _cancel_market_quotes(self, token_id: str) -> None:
        """Cancel all quotes for a market."""
        # NOTE: Pending buy reservations will be released when _on_order_update
        # receives CANCELLED confirmation from exchange (not cleared optimistically)
        cancelled = await self.order_manager.cancel_all_for_token(token_id)
        logger.info(f"Cancelled {cancelled} orders for {token_id[:20]}...")

        market = self._markets.get(token_id)
        if market:
            market.last_quote = None
            market.is_quoting = False

    def _check_refresh_rate(self, token_id: str) -> bool:
        """Check if refresh is allowed based on rate limits."""
        now = datetime.utcnow()

        # Per-market rate limit
        last_refresh = self._last_quote_refresh.get(token_id)
        if last_refresh:
            elapsed_ms = (now - last_refresh).total_seconds() * 1000
            if elapsed_ms < self.config.min_refresh_interval_ms:
                return False

        # Global rate limit
        now_ts = now.timestamp()
        if now_ts - self._global_refresh_window_start >= 1.0:
            # New window
            self._global_refresh_window_start = now_ts
            self._global_refresh_count = 0

        if self._global_refresh_count >= self.config.global_refresh_cap_per_sec:
            return False

        self._global_refresh_count += 1
        return True

    # --- Markout Processing ---

    async def _markout_loop(self) -> None:
        """Background loop to capture markouts."""
        while self._running:
            try:
                # Get mid price lookup function
                def get_mid_price(token_id: str) -> Optional[float]:
                    orderbook = self.orderbook_manager.get_orderbook(token_id)
                    return orderbook.mid_price() if orderbook else None

                # Process due markouts
                captured = self.fill_analytics.process_markout_captures(get_mid_price)
                if captured:
                    logger.debug(f"Captured {len(captured)} markouts")

                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in markout loop: {e}")
                await asyncio.sleep(1)

    # --- Event Callbacks ---

    async def _on_book_update(self, token_id: str, orderbook: OrderbookState) -> None:
        """Handle orderbook update from market WebSocket."""
        self.risk_manager.update_feed_timestamp(token_id)

        market = self._markets.get(token_id)
        if market:
            market.orderbook = orderbook

        # Check for sweeps
        await self.momentum_detector.on_orderbook_update(orderbook)

    async def _on_trade(
        self,
        token_id: str,
        price: float,
        timestamp: datetime,
    ) -> None:
        """Handle trade event for momentum detection."""
        orderbook = self.orderbook_manager.get_orderbook(token_id)
        tick_size = orderbook.tick_size if orderbook else 0.01

        await self.momentum_detector.on_trade(
            token_id=token_id,
            price=price,
            tick_size=tick_size,
            timestamp=timestamp,
        )

    async def _on_tick_size_change(self, token_id: str, new_tick_size: float) -> None:
        """Handle tick size change event."""
        logger.warning(f"Tick size changed for {token_id[:20]}...: {new_tick_size}")
        # Orderbook manager already updates the state

    async def _on_fill(self, fill: Fill) -> None:
        """Handle fill event from user WebSocket."""
        # Log fill to event ledger (Phase 5)
        self.event_ledger.log_fill(
            order_id=fill.order_id,
            token_id=fill.token_id,
            side=fill.side.value,
            price=fill.price,
            size=fill.size,
            fee=fill.fee,
            trade_id=fill.trade_id,
            ws_sequence=fill.ws_sequence,
        )

        # Capture position BEFORE fill for P&L calculation
        position_before = self.inventory_manager.get_position(fill.token_id)
        position_size_before = position_before.size if position_before else 0.0
        avg_entry_before = position_before.avg_entry_price if position_before else 0.0

        # Update inventory
        self.inventory_manager.update_from_fill(fill)

        # Track P&L with the PnL tracker (logs buy/sell with profit info)
        position_after = self.inventory_manager.get_position(fill.token_id)
        trade_result = self.pnl_tracker.record_fill(
            fill=fill,
            position=position_after,
            position_before_fill=position_size_before,
        )

        # Get mid price at fill time (fallback to fill.price if unavailable)
        orderbook = self.orderbook_manager.get_orderbook(fill.token_id)
        mid_price = fill.price  # Default fallback
        if orderbook:
            ob_mid = orderbook.mid_price()
            if ob_mid is not None:
                mid_price = ob_mid

        # Record fill for markout tracking
        record = self.fill_analytics.record_fill(
            fill=fill,
            mid_price_at_fill=mid_price,
            schedule_markouts=True,
        )

        # Update risk manager with P&L
        position = self.inventory_manager.get_position(fill.token_id)
        self.risk_manager.update_from_position(
            token_id=fill.token_id,
            position=position,
            current_price=mid_price,
        )

        # Get market name for alerts
        market_name = self._market_names.get(fill.token_id, fill.token_id[:40])

        # Persist fill and position to database (Phase 6)
        if self.persistence.is_enabled:
            self.persistence.save_fill_record(record, market_name=market_name)
            self.persistence.save_position(position, market_name=market_name)

        # Send fill alert with throttling (Phase 6)
        # Include P&L info for sells
        if self._enable_alerts:
            pnl = trade_result.net_pnl if trade_result else None
            entry_price = trade_result.avg_buy_price if trade_result else None
            session_pnl = self.pnl_tracker.session.net_pnl
            send_active_quoting_fill_alert(
                market_name=market_name,
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                pnl=pnl,
                entry_price=entry_price,
                session_pnl=session_pnl,
            )

    async def _on_order_update(self, order_state) -> None:
        """
        Handle order status update from user WebSocket.
        
        This is called when the exchange confirms order state changes.
        We use this to:
        1. Sync order_manager's local state with authoritative user channel state
        2. Release pending buy reservations on terminal states (confirmed by exchange)
        """
        logger.debug(f"Order update: {order_state.order_id} -> {order_state.status.value}")

        # Log order update to event ledger (Phase 5)
        self.event_ledger.log_order_update(
            order_id=order_state.order_id,
            token_id=order_state.token_id,
            side=order_state.side.value,
            status=order_state.status.value,
            original_size=order_state.original_size,
            remaining_size=order_state.remaining_size,
            ws_sequence=order_state.ws_sequence,
        )
        
        # Sync order_manager's state with authoritative user channel state
        self.order_manager.update_order_state(order_state.order_id, order_state.status)
        
        # Handle terminal states - release pending buy reservations
        # Only release when we have CONFIRMED terminal state from exchange
        terminal_states = (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)
        if order_state.status in terminal_states:
            # Only release if this was a BUY order (we only reserve for buys)
            if order_state.side == OrderSide.BUY:
                # Release the remaining (unfilled) size
                size_to_release = order_state.remaining_size
                if size_to_release > 0:
                    self.inventory_manager.release_pending_buy(
                        order_state.token_id, 
                        size_to_release
                    )
                    logger.info(
                        f"Released {size_to_release:.2f} pending buy for {order_state.token_id[:20]}... "
                        f"(order {order_state.order_id[:12]}... {order_state.status.value})"
                    )
            else:
                logger.debug(
                    f"Order {order_state.order_id[:12]}... terminal: {order_state.status.value} (SELL, no reservation)"
                )

    async def _on_market_ws_disconnect(self) -> None:
        """Handle market WebSocket disconnect."""
        logger.warning("Market WebSocket disconnected")
        await self.risk_manager.on_market_disconnect()

    async def _on_user_ws_disconnect(self) -> None:
        """Handle user WebSocket disconnect."""
        logger.error("User WebSocket disconnected - CRITICAL")
        # Force reconcile clears pending fills - positions will be understated until
        # next API sync. This is acceptable because:
        # 1. Risk manager halts trading on user disconnect anyway
        # 2. Next API poll (every few seconds) will restore accurate positions
        # 3. Being conservative (understating) is safer than overstating
        self.inventory_manager.force_reconcile_all()
        await self.risk_manager.on_user_disconnect()

    async def _on_momentum_detected(self, event) -> None:
        """Handle momentum detection event."""
        logger.warning(
            f"Momentum detected on {event.token_id[:20]}...: "
            f"{event.event_type} - {event.details}"
        )

        # Cancel quotes for this market if configured
        if self.config.cancel_on_momentum:
            await self._cancel_market_quotes(event.token_id)

    async def _on_circuit_breaker_state_change(
        self,
        old_state: CircuitBreakerState,
        new_state: CircuitBreakerState,
        reason: str,
    ) -> None:
        """Handle circuit breaker state change."""
        logger.warning(
            f"Circuit breaker: {old_state.value} -> {new_state.value} ({reason})"
        )

        # Send circuit breaker alert (Phase 6)
        if self._enable_alerts:
            risk_summary = self.risk_manager.get_summary()
            send_active_quoting_circuit_breaker_alert(
                old_state=old_state.value,
                new_state=new_state.value,
                reason=reason,
                details={
                    "drawdown": risk_summary.get("global", {}).get("current_drawdown", 0),
                    "consecutive_errors": risk_summary.get("global", {}).get("consecutive_errors", 0),
                    "stale_markets": len(risk_summary.get("stale_markets", [])),
                    "halt_reason": risk_summary.get("halt_reason", "NONE"),
                },
            )

    async def _on_market_halt(self, token_id: str, reason: str) -> None:
        """Handle market-specific halt."""
        logger.warning(f"Market halted: {token_id[:20]}... ({reason})")
        await self._cancel_market_quotes(token_id)

        # Send market halt alert (Phase 6)
        if self._enable_alerts:
            market_name = self._market_names.get(token_id, token_id[:40])
            send_active_quoting_market_halt_alert(
                market_name=market_name,
                reason=reason,
            )

    async def _on_kill_switch(self) -> None:
        """Handle kill switch - cancel all orders immediately."""
        logger.error("KILL SWITCH TRIGGERED - Cancelling all orders")
        self.inventory_manager.clear_all_pending_buys()
        await self.order_manager.cancel_all()

    def _on_markout_captured(self, sample) -> None:
        """Handle markout capture event."""
        logger.debug(
            f"Markout captured: {sample.fill_id} at {sample.horizon_seconds}s = "
            f"{sample.markout_bps:.2f} bps"
        )

        # Persist markout to database (Phase 6)
        if self.persistence.is_enabled:
            self.persistence.save_markout(sample)

    # --- Market Resolution Summaries ---

    async def _check_market_resolutions(self) -> None:
        """
        Check for markets that have ended and send resolution summaries.

        This sends a Telegram summary of how we did on each 15-minute market
        when the market's end_time passes.
        """
        if not self._enable_alerts:
            return

        now = datetime.now(timezone.utc)

        for token_id, end_time in list(self._market_end_times.items()):
            # Skip if already summarized
            if token_id in self._markets_summarized:
                continue

            # Ensure end_time is timezone-aware for comparison
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)

            # Check if market has ended
            if now < end_time:
                continue

            # Get market stats from PnL tracker
            market_summary = self.pnl_tracker.get_market_summary(token_id)
            if not market_summary:
                # No trades on this market, skip
                self._markets_summarized.add(token_id)
                continue

            # Get market name
            market_name = self._market_names.get(token_id, token_id[:40])

            # Get market-specific stats
            stats = self.pnl_tracker._market_stats.get(token_id)
            if not stats:
                self._markets_summarized.add(token_id)
                continue

            # Send resolution summary
            logger.info(f"Sending resolution summary for {market_name}")
            send_active_quoting_market_resolution_summary(
                market_name=market_name,
                net_pnl=stats.net_pnl,
                gross_pnl=stats.gross_pnl,
                total_fees=stats.total_fees,
                total_trades=stats.total_trades,
                buy_count=stats.total_buys,
                sell_count=stats.total_sells,
                winning_trades=stats.winning_trades,
                losing_trades=stats.losing_trades,
                buy_volume=stats.buy_volume,
                sell_volume=stats.sell_volume,
                session_pnl=self.pnl_tracker.session.net_pnl,
            )

            # Mark as summarized
            self._markets_summarized.add(token_id)

    # --- Wind-Down Strategy ---

    def _get_paired_token_id(self, token_id: str) -> Optional[str]:
        """
        Get the paired token ID (UP <-> DOWN) for a given token.

        In binary markets, both UP and DOWN tokens share the same condition_id.
        This method finds the other token in the pair.

        Args:
            token_id: Token ID to find pair for

        Returns:
            Paired token ID, or None if not found
        """
        condition_id = self.risk_manager._token_to_condition.get(token_id)
        if not condition_id:
            return None

        pair_state = self.risk_manager._market_pair_states.get(condition_id)
        if not pair_state:
            return None

        for tid in pair_state.token_ids:
            if tid != token_id:
                return tid
        return None

    def _get_wind_down_state(self, token_id: str) -> WindDownState:
        """
        Get the wind-down state for a market.

        Determines what phase we're in and calculates excess positions.

        Args:
            token_id: Token ID to check

        Returns:
            WindDownState with phase and excess position info
        """
        # Default state - normal operation
        default_state = WindDownState(
            phase=WindDownPhase.NORMAL,
            seconds_remaining=float("inf"),
        )

        # Get market end time
        end_time = self._market_end_times.get(token_id)
        if not end_time:
            return default_state

        # Ensure timezone awareness
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        seconds_remaining = (end_time - now).total_seconds()

        # Market already ended
        if seconds_remaining <= 0:
            return WindDownState(
                phase=WindDownPhase.MARKET_ENDED,
                seconds_remaining=0,
            )

        # Not yet in wind-down period
        if seconds_remaining > self.config.wind_down_start_seconds:
            return WindDownState(
                phase=WindDownPhase.NORMAL,
                seconds_remaining=seconds_remaining,
            )

        # Get positions for this token and its pair
        my_position = self.inventory_manager.get_position(token_id)
        my_size = my_position.size

        paired_token_id = self._get_paired_token_id(token_id)
        paired_size = 0.0
        if paired_token_id:
            paired_position = self.inventory_manager.get_position(paired_token_id)
            paired_size = paired_position.size

        # Calculate excess: positive means this token has more, negative means pair has more
        excess = my_size - paired_size

        # Determine which side has excess
        if excess > 0:
            excess_token_id = token_id
            excess_size = excess
            avg_entry_price = my_position.avg_entry_price
        elif excess < 0:
            excess_token_id = paired_token_id
            excess_size = -excess
            # Get paired position's entry price
            if paired_token_id:
                paired_position = self.inventory_manager.get_position(paired_token_id)
                avg_entry_price = paired_position.avg_entry_price
            else:
                avg_entry_price = 0.5
        else:
            # Positions are matched - no excess
            excess_token_id = None
            excess_size = 0.0
            avg_entry_price = 0.0

        # Get current price for excess token
        current_price = None
        if excess_token_id:
            orderbook = self.orderbook_manager.get_orderbook(excess_token_id)
            if orderbook and orderbook.best_bid is not None:
                current_price = orderbook.best_bid

        # Determine phase
        if seconds_remaining <= self.config.wind_down_taker_threshold_seconds:
            phase = WindDownPhase.TAKER_EXIT
        else:
            phase = WindDownPhase.WIND_DOWN

        return WindDownState(
            phase=phase,
            seconds_remaining=seconds_remaining,
            excess_token_id=excess_token_id,
            excess_size=excess_size,
            avg_entry_price=avg_entry_price,
            paired_token_id=paired_token_id,
            current_price=current_price,
        )

    async def _process_wind_down(self, token_id: str, wind_down: WindDownState) -> bool:
        """
        Process wind-down strategy for a market.

        Phase 1 (WIND_DOWN): No buys, maker-only sells at profitable prices
        Phase 2 (TAKER_EXIT): Taker sell excess if price < threshold

        Args:
            token_id: Token being processed
            wind_down: Current wind-down state

        Returns:
            True if wind-down handled the market (skip normal processing),
            False if normal processing should continue
        """
        # Log wind-down state periodically (every 30 seconds to avoid spam)
        now = time.time()
        last_log = self._wind_down_logged.get(token_id, 0)
        if now - last_log > 30:
            market_name = self._market_names.get(token_id, token_id[:20])
            if wind_down.excess_size > 0:
                logger.info(
                    f"Wind-down [{wind_down.phase.value}] {market_name}: "
                    f"{wind_down.seconds_remaining:.0f}s left, "
                    f"excess={wind_down.excess_size:.0f} on {wind_down.excess_token_id[:20] if wind_down.excess_token_id else 'N/A'}..."
                )
            else:
                logger.info(
                    f"Wind-down [{wind_down.phase.value}] {market_name}: "
                    f"{wind_down.seconds_remaining:.0f}s left, positions matched"
                )
            self._wind_down_logged[token_id] = now

        # Phase 1: Wind-down - maker sells only
        if wind_down.phase == WindDownPhase.WIND_DOWN:
            # On first entry to wind-down, cancel ALL orders for both tokens in the pair
            condition_id = self.risk_manager._token_to_condition.get(token_id)
            if condition_id and condition_id not in self._wind_down_orders_cancelled:
                market_name = self._market_names.get(token_id, token_id[:20])
                logger.warning(f"WIND-DOWN ENTRY {market_name}: Cancelling all orders for market pair")

                # Cancel orders for this token
                await self._cancel_market_quotes(token_id)

                # Cancel orders for paired token too
                if wind_down.paired_token_id:
                    await self._cancel_market_quotes(wind_down.paired_token_id)

                self._wind_down_orders_cancelled.add(condition_id)

            # Place sell orders if we have excess and can sell profitably
            if wind_down.excess_token_id == token_id and wind_down.excess_size > 0:
                await self._place_wind_down_sell(token_id, wind_down)

            return True  # Skip normal processing

        # Phase 2: Taker exit
        if wind_down.phase == WindDownPhase.TAKER_EXIT:
            # Cancel any existing quotes
            market = self._markets.get(token_id)
            if market and market.is_quoting:
                await self._cancel_market_quotes(token_id)

            # Check if taker exit already executed for this market pair
            condition_id = self.risk_manager._token_to_condition.get(token_id)
            if condition_id and condition_id in self._wind_down_taker_executed:
                return True  # Already handled

            # Execute taker exit if conditions are met
            if wind_down.excess_token_id and wind_down.excess_size > 0:
                if wind_down.current_price is not None:
                    if wind_down.current_price < self.config.wind_down_taker_price_threshold:
                        await self._execute_taker_exit(wind_down)
                        if condition_id:
                            self._wind_down_taker_executed.add(condition_id)
                    else:
                        # Price too high - hold to resolution
                        market_name = self._market_names.get(token_id, token_id[:20])
                        if now - last_log > 30:
                            logger.info(
                                f"Wind-down HOLD {market_name}: "
                                f"price ${wind_down.current_price:.4f} >= ${self.config.wind_down_taker_price_threshold:.2f} threshold, "
                                f"holding {wind_down.excess_size:.0f} shares to resolution"
                            )

            return True  # Skip normal processing

        # Normal phase or market ended - let normal processing handle it
        return False

    async def _place_wind_down_sell(self, token_id: str, wind_down: WindDownState) -> None:
        """
        Place maker sell orders during wind-down to reduce excess position.

        Only places sells if the price would be profitable (price > avg_entry_price).

        Args:
            token_id: Token to sell
            wind_down: Wind-down state with excess info
        """
        if wind_down.excess_size <= 0:
            return

        # Get orderbook
        orderbook = self.orderbook_manager.get_orderbook(token_id)
        if not orderbook or not orderbook.is_valid():
            return

        best_ask = orderbook.best_ask
        best_bid = orderbook.best_bid
        if best_ask is None or best_bid is None:
            return

        # To be a maker sell, we must place ABOVE best_bid to avoid crossing
        # Place at best_ask to be at front of queue
        sell_price = best_ask

        # Safety check: ensure our sell won't cross the book
        if sell_price <= best_bid:
            # Would cross the book - try placing 1 tick above best_bid
            tick_size = 0.01
            sell_price = best_bid + tick_size

        # Check if we can sell profitably
        if sell_price <= wind_down.avg_entry_price:
            # Can't sell profitably at maker price, skip
            return

        # Calculate sell size - sell enough to match positions, respecting minimums
        min_size = 5.0
        sell_size = min(wind_down.excess_size, self.config.order_size_usdc)
        if sell_size < min_size:
            sell_size = min(wind_down.excess_size, min_size)
            if sell_size < min_size:
                return  # Not enough to sell

        # Place maker sell order
        market_name = self._market_names.get(token_id, token_id[:20])
        logger.info(
            f"Wind-down SELL {market_name}: "
            f"{sell_size:.0f} @ ${sell_price:.4f} (entry: ${wind_down.avg_entry_price:.4f})"
        )

        result = await self.order_manager.place_order(
            token_id=token_id,
            side=OrderSide.SELL,
            price=sell_price,
            size=sell_size,
            neg_risk=False,
        )

        if result.success:
            # Register placed order for fill verification
            if result.order_id:
                self.user_channel_manager.register_placed_order(result.order_id)
            market = self._markets.get(token_id)
            if market:
                market.is_quoting = True
            self.risk_manager.clear_errors()
        else:
            logger.warning(f"Wind-down sell failed: {result.error_msg}")

    async def _execute_taker_exit(self, wind_down: WindDownState) -> None:
        """
        Execute a taker (market) sell to exit excess position.

        Called when:
        - Less than 40 seconds remaining
        - Excess position price < $0.25 threshold

        Args:
            wind_down: Wind-down state with excess info
        """
        if not wind_down.excess_token_id or wind_down.excess_size <= 0:
            return

        token_id = wind_down.excess_token_id
        market_name = self._market_names.get(token_id, token_id[:20])

        # Get orderbook to find best bid (we're selling, so we hit the bid)
        orderbook = self.orderbook_manager.get_orderbook(token_id)
        if not orderbook or orderbook.best_bid is None:
            logger.warning(f"Taker exit failed - no orderbook for {market_name}")
            return

        # Sell at best bid (taker)
        sell_price = orderbook.best_bid
        sell_size = wind_down.excess_size

        # Ensure minimum order size
        min_size = 5.0
        if sell_size < min_size:
            sell_size = min_size

        logger.warning(
            f"TAKER EXIT {market_name}: "
            f"SELL {sell_size:.0f} @ ${sell_price:.4f} "
            f"(price < ${self.config.wind_down_taker_price_threshold:.2f}, "
            f"{wind_down.seconds_remaining:.0f}s remaining)"
        )

        # Place non-post-only sell (taker order)
        # We need to temporarily disable post_only for this order
        result = await self.order_manager.place_order(
            token_id=token_id,
            side=OrderSide.SELL,
            price=sell_price,
            size=sell_size,
            neg_risk=False,
            post_only=False,  # CRITICAL: This is a taker order
        )

        if result.success:
            # Register placed order for fill verification
            if result.order_id:
                self.user_channel_manager.register_placed_order(result.order_id)
            logger.info(f"Taker exit order placed for {market_name}")
            self.risk_manager.clear_errors()
        else:
            logger.error(f"Taker exit failed for {market_name}: {result.error_msg}")

    # --- Redemption Handling ---

    async def _check_redemptions(self) -> None:
        """Check for markets ready for redemption and trigger redemptions."""
        # Get markets ready to check
        ready_markets = self.redemption_manager.get_markets_ready_for_check()

        for state in ready_markets:
            token_id = state.token_id

            # Get current position size from inventory manager
            position = self.inventory_manager.get_position(token_id)
            current_size = position.size

            # Update redemption manager with current position
            self.redemption_manager.update_position_size(token_id, current_size)

            # Skip if no position
            if current_size <= 0:
                logger.info(
                    f"No position to redeem for {token_id[:20]}... (size: {current_size})"
                )
                continue

            # Cancel any open orders for this market before redemption
            market = self._markets.get(token_id)
            if market and market.is_quoting:
                logger.info(f"Cancelling quotes for {token_id[:20]}... before redemption")
                await self._cancel_market_quotes(token_id)

            # Attempt redemption
            await self.redemption_manager.attempt_redemption(token_id, current_size)

    async def _on_redemption_complete(
        self,
        token_id: str,
        condition_id: str,
        tx_hash: str,
        position_size: float,
    ) -> None:
        """Handle successful redemption."""
        logger.info(
            f"Redemption complete for {token_id[:20]}...: "
            f"size={position_size:.2f}, tx={tx_hash[:20] if tx_hash else 'N/A'}..."
        )

        # Clear position from inventory manager
        # Note: The actual balance will be updated on next position sync from API
        self.inventory_manager.clear_position(token_id)

        # Remove from active tokens (no longer need to quote on this market)
        self._active_tokens.discard(token_id)

        # Clear from persistence
        if self.persistence.is_enabled:
            self.persistence.clear_position(token_id)

        # Get market name for alert
        market_name = self._market_names.get(token_id, token_id[:40])

        # Send alert
        if self._enable_alerts:
            send_active_quoting_redemption_alert(
                market_name=market_name,
                position_size=position_size,
                tx_hash=tx_hash,
                success=True,
            )

    async def _on_redemption_error(
        self,
        token_id: str,
        condition_id: str,
        error_message: str,
    ) -> None:
        """Handle failed redemption."""
        logger.error(
            f"Redemption failed for {token_id[:20]}...: {error_message}"
        )

        # Get position size for alert
        position = self.inventory_manager.get_position(token_id)
        position_size = position.size

        # Get market name for alert
        market_name = self._market_names.get(token_id, token_id[:40])

        # Send alert
        if self._enable_alerts:
            send_active_quoting_redemption_alert(
                market_name=market_name,
                position_size=position_size,
                success=False,
                error_message=error_message,
            )

    # --- Daily Summary Loop (Phase 6) ---

    async def _daily_summary_loop(self) -> None:
        """Background loop to send daily summary alerts."""
        # Send summary every 24 hours
        summary_interval_seconds = 24 * 60 * 60  # 24 hours

        while self._running:
            try:
                # Wait for next summary time
                await asyncio.sleep(summary_interval_seconds)

                if not self._running:
                    break

                # Send daily summary
                await self._send_daily_summary()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in daily summary loop: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute

    async def _send_daily_summary(self) -> None:
        """Send daily summary alert."""
        if not self._enable_alerts:
            return

        # Calculate session duration
        session_hours = 0.0
        if self._start_time:
            elapsed = datetime.now(timezone.utc) - self._start_time
            session_hours = elapsed.total_seconds() / 3600.0

        # Get analytics summary
        analytics = self.fill_analytics.get_summary()

        # Prepare markout stats
        markout_stats = {}
        markouts = analytics.get("markouts", {})
        for horizon_key, data in markouts.items():
            if isinstance(data, dict) and "avg" in data:
                horizon = int(horizon_key.replace("s", ""))
                avg_markout = data.get("avg")
                if avg_markout is not None:
                    # Convert to bps
                    markout_stats[horizon] = avg_markout * 10000

        send_active_quoting_daily_summary(
            session_duration_hours=session_hours,
            total_fills=analytics.get("total_fills", 0),
            total_volume=analytics.get("total_volume", 0.0),
            total_notional=analytics.get("total_notional", 0.0),
            net_fees=analytics.get("net_fees", 0.0),
            realized_pnl=analytics.get("realized_pnl", 0.0),
            markout_stats=markout_stats if markout_stats else None,
            market_count=len(self._active_tokens),
        )

        # Update session stats in database
        if self.persistence.is_enabled:
            self.persistence.update_session_stats(
                total_fills=analytics.get("total_fills", 0),
                total_volume=analytics.get("total_volume", 0.0),
                total_notional=analytics.get("total_notional", 0.0),
                net_fees=analytics.get("net_fees", 0.0),
                realized_pnl=analytics.get("realized_pnl", 0.0),
            )

    # --- Status/Info ---

    def get_status(self) -> dict:
        """Get current bot status."""
        return {
            "running": self._running,
            "active_markets": len(self._active_tokens),
            "market_ws_connected": self.orderbook_manager.is_connected(),
            "user_ws_connected": self.user_channel_manager.is_connected(),
            "circuit_breaker_state": self.risk_manager.state.value,
            "open_orders": self.order_manager.get_open_order_count(),
            "positions": self.inventory_manager.get_summary(),
            "risk": self.risk_manager.get_summary(),
            "analytics": self.fill_analytics.get_summary(),
            "redemptions": self.redemption_manager.get_summary(),
            "event_ledger": self.event_ledger.get_summary(),
        }

    def is_running(self) -> bool:
        """Check if bot is running."""
        return self._running

    async def _send_status_via_telegram(self) -> None:
        """Send bot status via Telegram in response to /status command."""
        import subprocess
        from .alerts import send_alert

        # Check status of all bots
        def is_process_running(pattern: str) -> bool:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True
                )
                return bool(result.stdout.strip())
            except Exception:
                return False

        trading_running = is_process_running("python.*main.py")
        aq_running = self._running  # We know our own status
        gabagool_running = is_process_running("rebates.gabagool.run")

        # Bot status header
        message = "📊 <b>Bot Status</b>\n\n"
        message += "<b>Bots:</b>\n"
        message += f"  Trading: {'🟢 Running' if trading_running else '🔴 Stopped'}\n"
        message += f"  AQ: {'🟢 Running' if aq_running else '🟡 Paused'}\n"
        message += f"  Gabagool: {'🟢 Running' if gabagool_running else '🔴 Stopped'}\n\n"

        # AQ details (since we're the AQ bot)
        status = self.get_status()
        pnl_summary = self.pnl_tracker.session

        message += "<b>AQ Details:</b>\n"
        message += f"  Markets: {status['active_markets']}\n"
        message += f"  Circuit Breaker: {status['circuit_breaker_state']}\n"
        message += f"  Open Orders: {status['open_orders']}\n"
        message += f"  Session P&L: ${pnl_summary.net_pnl:+.2f}\n"
        message += f"  Total Trades: {pnl_summary.total_trades}\n"
        message += f"  Win Rate: {pnl_summary.win_rate:.0f}%\n\n"

        # WebSocket status
        message += "<b>Connections:</b>\n"
        message += f"  Market WS: {'🟢' if status['market_ws_connected'] else '🔴'}\n"
        message += f"  User WS: {'🟢' if status['user_ws_connected'] else '🔴'}"

        send_alert(message, wait=True)


# --- CLI Entry Point ---

def discover_markets(assets: List[str]) -> List[Dict[str, Any]]:
    """
    Discover upcoming 15-minute crypto markets for the given assets.

    Args:
        assets: List of asset symbols (e.g., ["btc", "eth", "sol"])

    Returns:
        List of market dicts with token info
    """
    from rebates.market_finder import CryptoMarketFinder

    finder = CryptoMarketFinder()
    all_markets = []

    for asset in assets:
        # Use get_live_and_upcoming_markets to include currently live markets
        markets = finder.get_live_and_upcoming_markets()
        # Filter to this asset
        asset_markets = [m for m in markets if m.get("_asset", "").lower() == asset.lower()]
        all_markets.extend(asset_markets)

    # Deduplicate by condition_id
    seen = set()
    unique_markets = []
    for m in all_markets:
        cid = m.get("conditionId")
        if cid and cid not in seen:
            seen.add(cid)
            unique_markets.append(m)

    return unique_markets


def get_token_ids_from_markets(
    markets: List[Dict[str, Any]]
) -> tuple[List[str], Dict[str, str], Dict[str, tuple[datetime, datetime]], Dict[str, str]]:
    """
    Extract token IDs, market names, time windows, and condition IDs from market data.

    Returns:
        Tuple of (token_ids, market_names dict, market_times dict, condition_ids dict)
        market_times maps token_id -> (start_time, end_time) in UTC
        condition_ids maps token_id -> condition_id for redemption
    """
    import json
    from datetime import datetime, timedelta

    token_ids = []
    market_names = {}
    market_times = {}  # token_id -> (start_time, end_time)
    condition_ids = {}  # token_id -> condition_id

    for market in markets:
        # Get tokens from clobTokenIds (may be JSON string or list)
        clob_tokens = market.get("clobTokenIds", [])
        question = market.get("question", "Unknown")
        condition_id = market.get("conditionId", "")

        # Parse if it's a JSON string
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except json.JSONDecodeError:
                clob_tokens = []

        if not isinstance(clob_tokens, list):
            continue

        # Parse end date from market data (ISO format)
        end_time = None
        start_time = None
        end_date_str = market.get("endDate")
        if end_date_str:
            try:
                # Parse ISO format: 2026-01-13T06:15:00Z
                end_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                # Remove timezone info for comparison with UTC
                end_time = end_time.replace(tzinfo=None)
                # Start time is 15 minutes before end for 15-min markets
                start_time = end_time - timedelta(minutes=15)
            except (ValueError, AttributeError):
                pass

        for token_id in clob_tokens:
            if token_id and isinstance(token_id, str):
                token_ids.append(token_id)
                market_names[token_id] = question
                if start_time and end_time:
                    market_times[token_id] = (start_time, end_time)
                if condition_id:
                    condition_ids[token_id] = condition_id

    return token_ids, market_names, market_times, condition_ids


async def run_bot(
    token_ids: Optional[List[str]] = None,
    config: Optional[ActiveQuotingConfig] = None,
) -> None:
    """
    Run the active quoting bot.

    Args:
        token_ids: Optional list of token IDs (auto-discovers if not provided)
        config: Optional configuration (uses env vars if not provided)
    """
    # Load config
    if config is None:
        config = ActiveQuotingConfig.from_env()

    # Get credentials using PolymarketClient (derives from PK env var)
    try:
        from poly_data.polymarket_client import PolymarketClient
        poly_client = PolymarketClient()
        creds = poly_client.client.creds
        api_key = creds.api_key
        api_secret = creds.api_secret
        api_passphrase = creds.api_passphrase
    except Exception as e:
        logger.error(f"Failed to get API credentials: {e}")
        logger.error("Make sure PK environment variable is set with your private key")
        return

    market_names = {}
    market_times = {}
    condition_ids = {}

    # Auto-discover markets if no tokens provided
    if not token_ids:
        logger.info(f"Discovering markets for assets: {config.assets}")
        markets = discover_markets(config.assets)

        if not markets:
            logger.error("No upcoming markets found. Exiting.")
            return

        token_ids, market_names, market_times, condition_ids = get_token_ids_from_markets(markets)

        if not token_ids:
            logger.error("No token IDs found in discovered markets. Exiting.")
            return

        logger.info(f"Discovered {len(token_ids)} tokens from {len(markets)} markets:")
        for market in markets:
            logger.info(f"  - {market.get('question', 'Unknown')}")

    # Create and run bot
    bot = ActiveQuotingBot(
        config=config,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        poly_client=poly_client,
    )

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    # SIGHUP for screen -X quit, SIGINT/SIGTERM for manual stop
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await bot.start(
            token_ids,
            market_names=market_names,
            market_times=market_times,
            condition_ids=condition_ids,
        )

        # Main loop - periodically check for new markets
        check_interval = 60  # Check for new markets every 60 seconds

        while bot.is_running() and not shutdown_event.is_set():
            # Use wait_for with timeout so we can check shutdown_event
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=check_interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue loop

            # Discover new markets
            new_markets = discover_markets(config.assets)
            new_token_ids, new_names, new_times, new_cids = get_token_ids_from_markets(new_markets)

            # Find tokens we're not already tracking
            current_tokens = bot._active_tokens
            added = []
            for tid in new_token_ids:
                if tid not in current_tokens:
                    added.append(tid)
                    if tid in new_names:
                        market_names[tid] = new_names[tid]
                    if tid in new_times:
                        market_times[tid] = new_times[tid]
                    if tid in new_cids:
                        condition_ids[tid] = new_cids[tid]

            if added:
                logger.info(f"Discovered {len(added)} new tokens, adding to bot")
                # TODO: Implement hot-add of new tokens
                # For now, just log - would need bot.add_tokens() method

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await bot.stop()


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Active Quoting Bot for Polymarket 15-minute crypto markets"
    )
    parser.add_argument(
        "--tokens",
        nargs="+",
        help="Token IDs to quote on (optional - auto-discovers if not provided)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no real orders)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Set dry run mode
    if args.dry_run:
        os.environ["AQ_DRY_RUN"] = "true"

    # Run (tokens are optional now - will auto-discover)
    asyncio.run(run_bot(args.tokens))


if __name__ == "__main__":
    main()
