"""
UserChannelManager - Authenticated user WebSocket for order/fill state.

Handles the following events from the Polymarket user WebSocket:
- Order fills
- Order cancellations
- Order status updates

This is CRITICAL for maintaining authoritative order state. Without this,
the bot will eventually desync (think orders are open when they're not).
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Callable, Awaitable, List, Set
import websockets
from websockets.exceptions import ConnectionClosed

from .config import ActiveQuotingConfig
from .models import OrderState, OrderStatus, OrderSide, Fill

logger = logging.getLogger(__name__)


class UserChannelManager:
    """
    Manages authenticated user WebSocket connection for order/fill state.

    This class:
    1. Connects to the Polymarket user WebSocket with authentication
    2. Receives and processes order status updates
    3. Maintains authoritative order state
    4. Triggers callbacks on fills and order status changes
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        wallet_address: Optional[str] = None,
        on_fill: Optional[Callable[[Fill], Awaitable[None]]] = None,
        on_order_update: Optional[Callable[[OrderState], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        Initialize the UserChannelManager.

        Args:
            config: Active quoting configuration
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            api_passphrase: Polymarket API passphrase
            wallet_address: Our wallet address (to verify we're the maker in fills)
            on_fill: Callback when a fill occurs
            on_order_update: Callback when order status changes
            on_disconnect: Callback when WebSocket disconnects
        """
        self.config = config
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._wallet_address = wallet_address.lower() if wallet_address else None

        self.on_fill = on_fill
        self.on_order_update = on_order_update
        self.on_disconnect = on_disconnect

        # State
        self._orders: Dict[str, OrderState] = {}  # order_id -> OrderState
        self._orders_by_token: Dict[str, Set[str]] = {}  # token_id -> set of order_ids
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self._last_message_time: Optional[datetime] = None
        self._authenticated: bool = False
        self._processed_trade_ids: Set[str] = set()  # Dedup fills by trade_id

    @property
    def orders(self) -> Dict[str, OrderState]:
        """Get all order states."""
        return self._orders

    def get_order(self, order_id: str) -> Optional[OrderState]:
        """Get order state by order ID."""
        return self._orders.get(order_id)

    def get_orders_for_token(self, token_id: str) -> List[OrderState]:
        """Get all orders for a specific token."""
        order_ids = self._orders_by_token.get(token_id, set())
        return [self._orders[oid] for oid in order_ids if oid in self._orders]

    def get_open_orders(self, token_id: Optional[str] = None) -> List[OrderState]:
        """Get all open orders, optionally filtered by token."""
        orders = self._orders.values()
        if token_id:
            order_ids = self._orders_by_token.get(token_id, set())
            orders = [self._orders[oid] for oid in order_ids if oid in self._orders]
        return [o for o in orders if o.is_open()]

    def is_connected(self) -> bool:
        """Check if WebSocket is connected and authenticated."""
        if self._websocket is None or not self._authenticated:
            return False
        try:
            # websockets 15.x uses state property
            from websockets import State
            return self._websocket.state == State.OPEN
        except (ImportError, AttributeError):
            # Fallback for older versions
            return getattr(self._websocket, 'open', False)

    def last_update_time(self) -> Optional[datetime]:
        """Get the time of the last received message."""
        return self._last_message_time

    async def connect(self) -> None:
        """Connect to the user WebSocket and authenticate."""
        self._running = True
        await self._connect_and_run()

    async def _connect_and_run(self) -> None:
        """Internal method to connect and run the WebSocket loop."""
        while self._running and self._reconnect_attempts < self.config.ws_max_reconnect_attempts:
            try:
                async with websockets.connect(
                    self.config.user_ws_uri,
                    ping_interval=self.config.ws_ping_interval,
                    ping_timeout=None,
                ) as websocket:
                    self._websocket = websocket
                    self._reconnect_attempts = 0
                    logger.info(f"Connected to user WebSocket: {self.config.user_ws_uri}")

                    # Authenticate
                    auth_message = {
                        "type": "user",
                        "auth": {
                            "apiKey": self._api_key,
                            "secret": self._api_secret,
                            "passphrase": self._api_passphrase,
                        },
                    }
                    await websocket.send(json.dumps(auth_message))
                    logger.info("Sent user WebSocket authentication")

                    # Wait for auth confirmation
                    self._authenticated = True  # Assume success, will update on error

                    # Process messages
                    await self._message_loop(websocket)

            except ConnectionClosed as e:
                logger.warning(f"User WebSocket connection closed: {e}")
                self._authenticated = False
                if self.on_disconnect:
                    await self.on_disconnect()
            except Exception as e:
                logger.error(f"User WebSocket error: {e}")
                self._authenticated = False
                if self.on_disconnect:
                    await self.on_disconnect()

            self._websocket = None
            self._authenticated = False

            if self._running:
                self._reconnect_attempts += 1
                delay = self.config.ws_reconnect_delay_seconds
                logger.info(f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})")
                await asyncio.sleep(delay)

        if self._reconnect_attempts >= self.config.ws_max_reconnect_attempts:
            logger.error("Max reconnect attempts reached for user WebSocket")
            self._running = False

    async def _message_loop(self, websocket: websockets.WebSocketClientProtocol) -> None:
        """Process incoming WebSocket messages."""
        async for message in websocket:
            try:
                self._last_message_time = datetime.utcnow()
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON from user WebSocket: {e}")
            except Exception as e:
                logger.error(f"Error processing user WebSocket message: {e}")

    async def _handle_message(self, data) -> None:
        """
        Handle a parsed WebSocket message.

        Expected message types from user channel:
        - order: Order status updates
        - trade: Fill notifications
        """
        # Handle case where we receive a list of messages
        if isinstance(data, list):
            for item in data:
                await self._handle_single_message(item)
        else:
            await self._handle_single_message(data)

    def _extract_ws_sequence(self, data: Dict) -> Optional[int]:
        """Extract a WebSocket sequence number if present in the message."""
        if not isinstance(data, dict):
            return None

        for key in (
            "sequence",
            "seq",
            "sequence_number",
            "sequenceNumber",
            "message_seq",
            "message_sequence",
            "seq_num",
            "seqNum",
        ):
            if key in data:
                try:
                    return int(data.get(key))
                except (TypeError, ValueError):
                    return None
        return None

    async def _handle_single_message(self, data: Dict) -> None:
        """Handle a single parsed message."""
        if not isinstance(data, dict):
            return

        # Check for authentication error
        if data.get("error"):
            logger.error(f"User WebSocket error: {data.get('error')}")
            self._authenticated = False
            return

        # Check for explicit auth success
        if data.get("type") == "auth" and data.get("status") == "success":
            self._authenticated = True
            logger.info("User WebSocket authenticated successfully")
            return

        # Skip heartbeat/ping messages
        msg_type = data.get("type", "")
        if msg_type in ("heartbeat", "ping", "subscription_confirmation"):
            return

        event_type = data.get("event_type")

        ws_sequence = self._extract_ws_sequence(data)

        if event_type == "order":
            await self._handle_order_event(data, ws_sequence)
        elif event_type == "trade":
            await self._handle_trade_event(data, ws_sequence)
        # Also handle direct order/trade objects without event_type wrapper
        elif "order_id" in data and "status" in data:
            await self._handle_order_event(data, ws_sequence)
        # Handle Polymarket format: has 'id', 'asset_id', 'side', 'price', 'size', 'market'
        elif "id" in data and "asset_id" in data and "market" in data:
            # This is likely a trade event in Polymarket format
            await self._handle_trade_event(data, ws_sequence)
        elif "trade_id" in data or ("order_id" in data and "price" in data and "size" in data):
            await self._handle_trade_event(data, ws_sequence)
        else:
            # Log unknown message formats for debugging
            logger.debug(f"Unknown user WS message format: {list(data.keys())[:6]}")

    async def _handle_order_event(self, data: Dict, ws_sequence: Optional[int] = None) -> None:
        """Handle an order status update."""
        order_id = data.get("order_id") or data.get("id")
        if not order_id:
            return

        token_id = data.get("asset_id") or data.get("token_id")
        status_str = data.get("status", "").upper()
        side_str = data.get("side", "").upper()

        # Map status string to OrderStatus enum
        status_map = {
            "PENDING": OrderStatus.PENDING,
            "OPEN": OrderStatus.OPEN,
            "LIVE": OrderStatus.OPEN,
            "MATCHED": OrderStatus.PARTIALLY_FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "CANCELED": OrderStatus.CANCELLED,
            "EXPIRED": OrderStatus.EXPIRED,
            "REJECTED": OrderStatus.REJECTED,
        }
        status = status_map.get(status_str, OrderStatus.PENDING)

        # Map side string to OrderSide enum
        side_map = {
            "BUY": OrderSide.BUY,
            "SELL": OrderSide.SELL,
        }
        side = side_map.get(side_str, OrderSide.BUY)

        # Get or create order state
        if order_id in self._orders:
            order = self._orders[order_id]
            order.status = status
            order.updated_at = datetime.utcnow()
            if ws_sequence is not None:
                order.ws_sequence = ws_sequence

            # Update remaining size if provided
            if "remaining_size" in data or "size_matched" in data:
                original = float(data.get("original_size", order.original_size))
                matched = float(data.get("size_matched", 0))
                remaining = float(data.get("remaining_size", original - matched))
                order.remaining_size = remaining
        else:
            # Create new order state
            price = float(data.get("price", 0))
            original_size = float(data.get("original_size", data.get("size", 0)))
            remaining_size = float(data.get("remaining_size", original_size))
            fee_rate_bps = int(data.get("fee_rate_bps", 0))

            order = OrderState(
                order_id=order_id,
                token_id=token_id or "",
                side=side,
                price=price,
                original_size=original_size,
                remaining_size=remaining_size,
                status=status,
                fee_rate_bps=fee_rate_bps,
                ws_sequence=ws_sequence,
            )
            self._orders[order_id] = order

            # Track by token
            if token_id:
                if token_id not in self._orders_by_token:
                    self._orders_by_token[token_id] = set()
                self._orders_by_token[token_id].add(order_id)

        logger.debug(f"Order update: {order_id} status={status.value}")

        if self.on_order_update:
            await self.on_order_update(order)

        # Clean up completed orders after some time (optional)
        if order.is_done():
            self._maybe_cleanup_order(order_id, token_id)

    async def _handle_trade_event(self, data: Dict, ws_sequence: Optional[int] = None) -> None:
        """
        Handle a fill/trade event.

        Polymarket WebSocket trade format:
        {
            'market': ...,
            'side': 'BUY'/'SELL',
            'asset_id': token_id,
            'event_type': 'trade',
            'status': 'MATCHED'/'CONFIRMED'/'FAILED',
            'id': trade_id,
            'maker_orders': [...],
            'size': ...,
            'price': ...,
        }
        """
        # Debug log the raw message to understand format
        logger.debug(f"Trade event raw data: {list(data.keys())}")

        # Get trade ID - Polymarket uses 'id', not 'trade_id'
        trade_id = data.get("id") or data.get("trade_id")

        # Deduplicate fills - WebSocket can replay the same fill multiple times
        if trade_id and trade_id in self._processed_trade_ids:
            logger.debug(f"Skipping duplicate fill: trade_id={trade_id}")
            return
        if trade_id:
            self._processed_trade_ids.add(trade_id)

        # Get token ID
        token_id = data.get("asset_id") or data.get("token_id")

        # Only process MATCHED/CONFIRMED status (actual fills)
        # If status is not present, treat as valid fill (for simpler message formats)
        status = data.get("status", "").upper()
        if status and status not in ("MATCHED", "CONFIRMED"):
            logger.debug(f"Skipping trade event with status: {status}")
            return

        # Get price and size
        price = float(data.get("price", 0))
        size = float(data.get("size", data.get("match_size", 0)))
        fee = float(data.get("fee", data.get("maker_fee", 0)))
        side_str = data.get("side", "").upper()

        # Determine side
        side_map = {
            "BUY": OrderSide.BUY,
            "SELL": OrderSide.SELL,
        }
        side = side_map.get(side_str, OrderSide.BUY)

        # Try to find order_id from maker_orders if we're the maker
        order_id = data.get("order_id") or data.get("maker_order_id")
        maker_orders = data.get("maker_orders", [])
        is_our_fill = False

        # If side wasn't in the trade event, try to get it from the tracked order
        if side_str == "" and order_id and order_id in self._orders:
            side = self._orders[order_id].side

        if maker_orders:
            # Find our order in maker_orders by checking maker_address
            for maker_order in maker_orders:
                maker_address = maker_order.get("maker_address", "").lower()

                # Check if this is our order
                if self._wallet_address and maker_address == self._wallet_address:
                    is_our_fill = True
                    if "order_id" in maker_order:
                        order_id = maker_order["order_id"]
                    # Get matched amount for this specific maker order
                    size = float(maker_order.get("matched_amount", size))
                    price = float(maker_order.get("price", price))
                    logger.debug(f"Found our maker order: {order_id} size={size}")
                    break

            # If we have wallet address configured and this isn't our fill, skip it
            if self._wallet_address and not is_our_fill:
                logger.debug(f"Skipping fill - not our maker order (trade_id={trade_id})")
                return
        else:
            # No maker_orders in message - this might be a different format
            # Check if it's an order we're tracking
            if order_id and order_id in self._orders:
                is_our_fill = True

        # Use trade_id as fallback for order_id if still not found
        if not order_id:
            order_id = trade_id or "unknown"

        # Parse timestamp
        timestamp_str = data.get("timestamp") or data.get("created_at")
        if timestamp_str:
            try:
                if isinstance(timestamp_str, str):
                    if timestamp_str.endswith("Z"):
                        timestamp_str = timestamp_str[:-1] + "+00:00"
                    timestamp = datetime.fromisoformat(timestamp_str)
                else:
                    timestamp = datetime.utcnow()
            except Exception:
                timestamp = datetime.utcnow()
        else:
            timestamp = datetime.utcnow()

        fill = Fill(
            order_id=order_id,
            token_id=token_id or "",
            side=side,
            price=price,
            size=size,
            fee=fee,
            timestamp=timestamp,
            trade_id=trade_id,
            ws_sequence=ws_sequence,
        )

        logger.info(f"Fill: trade={trade_id} token={token_id[:20] if token_id else 'N/A'}... side={side.value} price={price} size={size}")

        # Update order state if we have it
        if order_id and order_id in self._orders:
            order = self._orders[order_id]
            order.fills.append(fill)
            order.remaining_size -= size
            if order.remaining_size <= 0:
                order.status = OrderStatus.FILLED
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
            order.updated_at = datetime.utcnow()

        if self.on_fill:
            await self.on_fill(fill)

    def _maybe_cleanup_order(self, order_id: str, token_id: Optional[str]) -> None:
        """
        Mark order for potential cleanup.

        For now, we keep orders in memory. In production, we might want to
        remove terminal orders after some time to prevent memory growth.
        """
        pass  # Keep for now, implement cleanup later if needed

    def add_order(self, order: OrderState) -> None:
        """
        Add an order to the tracked orders.

        Call this when placing new orders so we can track their state.
        """
        self._orders[order.order_id] = order
        if order.token_id:
            if order.token_id not in self._orders_by_token:
                self._orders_by_token[order.token_id] = set()
            self._orders_by_token[order.token_id].add(order.order_id)

    def remove_order(self, order_id: str) -> Optional[OrderState]:
        """Remove an order from tracking."""
        order = self._orders.pop(order_id, None)
        if order and order.token_id:
            token_orders = self._orders_by_token.get(order.token_id)
            if token_orders:
                token_orders.discard(order_id)
        return order

    def clear_orders(self) -> None:
        """Clear all tracked orders."""
        self._orders.clear()
        self._orders_by_token.clear()

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket."""
        self._running = False
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
        self._authenticated = False

    def reconcile_with_api_orders(self, api_orders: List[Dict]) -> None:
        """
        Reconcile internal state with orders fetched from REST API.

        This should be called periodically to catch any missed updates.

        Args:
            api_orders: List of order dicts from the REST API
        """
        api_order_ids = set()

        for api_order in api_orders:
            order_id = api_order.get("id") or api_order.get("order_id")
            if not order_id:
                continue

            api_order_ids.add(order_id)

            # Update or create order state
            status_str = api_order.get("status", "").upper()
            status_map = {
                "PENDING": OrderStatus.PENDING,
                "OPEN": OrderStatus.OPEN,
                "LIVE": OrderStatus.OPEN,
                "MATCHED": OrderStatus.PARTIALLY_FILLED,
                "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                "FILLED": OrderStatus.FILLED,
                "CANCELLED": OrderStatus.CANCELLED,
                "CANCELED": OrderStatus.CANCELLED,
                "EXPIRED": OrderStatus.EXPIRED,
                "REJECTED": OrderStatus.REJECTED,
            }
            status = status_map.get(status_str, OrderStatus.PENDING)

            if order_id in self._orders:
                # Update existing
                order = self._orders[order_id]
                if order.status != status:
                    logger.warning(
                        f"Reconcile: Order {order_id} status mismatch: "
                        f"local={order.status.value} api={status.value}"
                    )
                    order.status = status

                remaining = float(api_order.get("remaining_size", order.remaining_size))
                if abs(order.remaining_size - remaining) > 0.001:
                    logger.warning(
                        f"Reconcile: Order {order_id} size mismatch: "
                        f"local={order.remaining_size} api={remaining}"
                    )
                    order.remaining_size = remaining
            else:
                # Create new order from API data
                side_str = api_order.get("side", "").upper()
                side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
                token_id = api_order.get("asset_id", api_order.get("token_id", ""))

                order = OrderState(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    price=float(api_order.get("price", 0)),
                    original_size=float(api_order.get("original_size", api_order.get("size", 0))),
                    remaining_size=float(api_order.get("remaining_size", 0)),
                    status=status,
                )
                self._orders[order_id] = order

                if token_id:
                    if token_id not in self._orders_by_token:
                        self._orders_by_token[token_id] = set()
                    self._orders_by_token[token_id].add(order_id)

                logger.warning(f"Reconcile: Added missing order {order_id}")

        # Check for orders we think are open but aren't in API (may be filled/cancelled)
        for order_id, order in list(self._orders.items()):
            if order.is_open() and order_id not in api_order_ids:
                logger.warning(
                    f"Reconcile: Order {order_id} not in API response, "
                    f"marking as cancelled"
                )
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.utcnow()
