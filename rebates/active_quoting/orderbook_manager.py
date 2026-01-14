"""
OrderbookManager - Real-time orderbook tracking via market WebSocket.

Handles the following events from the Polymarket market WebSocket:
- book: Full orderbook snapshot
- price_change: Incremental book updates
- best_bid_ask: Low-latency top-of-book updates
- last_trade_price: Trade price updates for momentum detection
- tick_size_change: Tick size updates (critical - can change mid-session)
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Callable, Awaitable, List, Set
import websockets
from websockets.exceptions import ConnectionClosed

from .config import ActiveQuotingConfig
from .models import OrderbookState, OrderbookLevel

logger = logging.getLogger(__name__)


class OrderbookManager:
    """
    Manages real-time orderbook state via WebSocket connection.

    This class:
    1. Connects to the Polymarket market WebSocket
    2. Subscribes to updates for specified tokens
    3. Maintains up-to-date orderbook state for each token
    4. Provides callbacks for important events
    """

    def __init__(
        self,
        config: ActiveQuotingConfig,
        on_book_update: Optional[Callable[[str, OrderbookState], Awaitable[None]]] = None,
        on_trade: Optional[Callable[[str, float, datetime], Awaitable[None]]] = None,
        on_tick_size_change: Optional[Callable[[str, float], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        Initialize the OrderbookManager.

        Args:
            config: Active quoting configuration
            on_book_update: Callback when orderbook updates (token_id, new_state)
            on_trade: Callback when trade occurs (token_id, price, timestamp)
            on_tick_size_change: Callback when tick size changes (token_id, new_tick_size)
            on_disconnect: Callback when WebSocket disconnects
        """
        self.config = config
        self.on_book_update = on_book_update
        self.on_trade = on_trade
        self.on_tick_size_change = on_tick_size_change
        self.on_disconnect = on_disconnect

        # State
        self._orderbooks: Dict[str, OrderbookState] = {}
        self._subscribed_tokens: Set[str] = set()
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running: bool = False
        self._reconnect_attempts: int = 0
        self._last_message_time: Optional[datetime] = None

    @property
    def orderbooks(self) -> Dict[str, OrderbookState]:
        """Get all orderbook states."""
        return self._orderbooks

    def get_orderbook(self, token_id: str) -> Optional[OrderbookState]:
        """Get orderbook state for a specific token."""
        return self._orderbooks.get(token_id)

    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        if self._websocket is None:
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

    async def connect(self, token_ids: List[str]) -> None:
        """
        Connect to the market WebSocket and subscribe to tokens.

        Args:
            token_ids: List of token IDs to subscribe to
        """
        if not token_ids:
            logger.warning("No token IDs provided for subscription")
            return

        self._subscribed_tokens = set(token_ids)
        self._running = True

        # Initialize orderbook state for each token
        for token_id in token_ids:
            if token_id not in self._orderbooks:
                self._orderbooks[token_id] = OrderbookState(token_id=token_id)

        await self._connect_and_run()

    async def _connect_and_run(self) -> None:
        """Internal method to connect and run the WebSocket loop."""
        while self._running and self._reconnect_attempts < self.config.ws_max_reconnect_attempts:
            try:
                async with websockets.connect(
                    self.config.market_ws_uri,
                    ping_interval=self.config.ws_ping_interval,
                    ping_timeout=None,
                ) as websocket:
                    self._websocket = websocket
                    self._reconnect_attempts = 0
                    logger.info(f"Connected to market WebSocket: {self.config.market_ws_uri}")

                    # Send subscription message
                    subscription = {"assets_ids": list(self._subscribed_tokens)}
                    await websocket.send(json.dumps(subscription))
                    logger.info(f"Subscribed to {len(self._subscribed_tokens)} tokens")

                    # Process messages
                    await self._message_loop(websocket)

            except ConnectionClosed as e:
                logger.warning(f"Market WebSocket connection closed: {e}")
                if self.on_disconnect:
                    await self.on_disconnect()
            except Exception as e:
                logger.error(f"Market WebSocket error: {e}")
                if self.on_disconnect:
                    await self.on_disconnect()

            self._websocket = None

            if self._running:
                self._reconnect_attempts += 1
                delay = self.config.ws_reconnect_delay_seconds
                logger.info(f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})")
                await asyncio.sleep(delay)

        if self._reconnect_attempts >= self.config.ws_max_reconnect_attempts:
            logger.error("Max reconnect attempts reached for market WebSocket")
            self._running = False

    async def _message_loop(self, websocket: websockets.WebSocketClientProtocol) -> None:
        """Process incoming WebSocket messages."""
        async for message in websocket:
            try:
                self._last_message_time = datetime.utcnow()
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON from market WebSocket: {e}")
            except Exception as e:
                logger.error(f"Error processing market WebSocket message: {e}")

    async def _handle_message(self, data) -> None:
        """
        Handle a parsed WebSocket message.

        Message types:
        - book: Full orderbook snapshot
        - price_change: Incremental updates
        - best_bid_ask: Top of book only
        - last_trade_price: Trade prices
        - tick_size_change: Tick size updates
        """
        # Handle case where we receive a list of messages
        if isinstance(data, list):
            for item in data:
                await self._handle_single_message(item)
        else:
            await self._handle_single_message(data)

    async def _handle_single_message(self, data: Dict) -> None:
        """Handle a single parsed message."""
        if not isinstance(data, dict):
            return

        event_type = data.get("event_type")

        if event_type == "book":
            await self._handle_book_event(data)
        elif event_type == "price_change":
            await self._handle_price_change_event(data)
        elif event_type == "best_bid_ask":
            await self._handle_best_bid_ask_event(data)
        elif event_type == "last_trade_price":
            await self._handle_last_trade_price_event(data)
        elif event_type == "tick_size_change":
            await self._handle_tick_size_change_event(data)
        # Ignore unknown event types silently

    async def _handle_book_event(self, data: Dict) -> None:
        """Handle a full orderbook snapshot."""
        token_id = data.get("asset_id")
        if not token_id or token_id not in self._subscribed_tokens:
            return

        bids = self._parse_levels(data.get("bids", []))
        asks = self._parse_levels(data.get("asks", []))
        tick_size = float(data.get("min_tick_size", 0.01))
        timestamp_str = data.get("timestamp")

        # Sort bids descending by price, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        orderbook = self._orderbooks.get(token_id)
        if orderbook:
            orderbook.bids = bids
            orderbook.asks = asks
            orderbook.tick_size = tick_size
            orderbook.last_update_time = datetime.utcnow()

            if self.on_book_update:
                await self.on_book_update(token_id, orderbook)

    async def _handle_price_change_event(self, data: Dict) -> None:
        """Handle an incremental price change event.

        Supports two formats:
        1. price_changes array with asset_id inside each change
        2. asset_id at top level with changes array
        """
        # Track which tokens were updated so we can trigger callbacks
        updated_tokens = set()

        # Format 1: price_changes array with asset_id inside each change
        price_changes = data.get("price_changes", [])
        for change in price_changes:
            token_id = change.get("asset_id")
            if not token_id or token_id not in self._subscribed_tokens:
                continue

            orderbook = self._orderbooks.get(token_id)
            if not orderbook:
                continue

            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            side = change.get("side", "").upper()

            if side == "BUY":
                self._update_level(orderbook.bids, price, size, descending=True)
            elif side == "SELL":
                self._update_level(orderbook.asks, price, size, descending=False)

            orderbook.last_update_time = datetime.utcnow()
            updated_tokens.add(token_id)

        # Format 2: asset_id at top level with changes array
        top_level_token_id = data.get("asset_id")
        changes = data.get("changes", [])
        if top_level_token_id and top_level_token_id in self._subscribed_tokens and changes:
            orderbook = self._orderbooks.get(top_level_token_id)
            if orderbook:
                for change in changes:
                    price = float(change.get("price", 0))
                    size = float(change.get("size", 0))
                    side = change.get("side", "").upper()

                    if side == "BUY":
                        self._update_level(orderbook.bids, price, size, descending=True)
                    elif side == "SELL":
                        self._update_level(orderbook.asks, price, size, descending=False)

                orderbook.last_update_time = datetime.utcnow()
                updated_tokens.add(top_level_token_id)

        # Trigger callbacks for all updated tokens
        if self.on_book_update:
            for token_id in updated_tokens:
                orderbook = self._orderbooks.get(token_id)
                if orderbook:
                    await self.on_book_update(token_id, orderbook)

    async def _handle_best_bid_ask_event(self, data: Dict) -> None:
        """
        Handle a best bid/ask update (low-latency top of book).

        This is faster than full book updates for quote decisions.
        """
        token_id = data.get("asset_id")
        if not token_id or token_id not in self._subscribed_tokens:
            return

        orderbook = self._orderbooks.get(token_id)
        if not orderbook:
            return

        best_bid = data.get("best_bid")
        best_ask = data.get("best_ask")

        # Update top of book if provided
        if best_bid is not None:
            bid_price = float(best_bid.get("price", 0))
            bid_size = float(best_bid.get("size", 0))
            if bid_size > 0:
                if orderbook.bids and orderbook.bids[0].price == bid_price:
                    orderbook.bids[0] = OrderbookLevel(price=bid_price, size=bid_size)
                elif not orderbook.bids or bid_price > orderbook.bids[0].price:
                    orderbook.bids.insert(0, OrderbookLevel(price=bid_price, size=bid_size))

        if best_ask is not None:
            ask_price = float(best_ask.get("price", 0))
            ask_size = float(best_ask.get("size", 0))
            if ask_size > 0:
                if orderbook.asks and orderbook.asks[0].price == ask_price:
                    orderbook.asks[0] = OrderbookLevel(price=ask_price, size=ask_size)
                elif not orderbook.asks or ask_price < orderbook.asks[0].price:
                    orderbook.asks.insert(0, OrderbookLevel(price=ask_price, size=ask_size))

        orderbook.last_update_time = datetime.utcnow()

        if self.on_book_update:
            await self.on_book_update(token_id, orderbook)

    async def _handle_last_trade_price_event(self, data: Dict) -> None:
        """Handle a last trade price event for momentum detection."""
        token_id = data.get("asset_id")
        if not token_id or token_id not in self._subscribed_tokens:
            return

        price = data.get("price")
        if price is None:
            return

        price = float(price)
        timestamp = datetime.utcnow()

        # Update orderbook's last trade price
        orderbook = self._orderbooks.get(token_id)
        if orderbook:
            orderbook.last_trade_price = price
            orderbook.last_update_time = timestamp

        # Trigger callback for momentum detection
        if self.on_trade:
            await self.on_trade(token_id, price, timestamp)

    async def _handle_tick_size_change_event(self, data: Dict) -> None:
        """
        Handle a tick size change event.

        CRITICAL: Tick size can change mid-session and affects quote pricing.
        """
        token_id = data.get("asset_id")
        if not token_id or token_id not in self._subscribed_tokens:
            return

        new_tick_size = data.get("min_tick_size")
        if new_tick_size is None:
            return

        new_tick_size = float(new_tick_size)
        old_tick_size = None

        orderbook = self._orderbooks.get(token_id)
        if orderbook:
            old_tick_size = orderbook.tick_size
            orderbook.tick_size = new_tick_size
            orderbook.last_update_time = datetime.utcnow()

        logger.warning(
            f"Tick size changed for {token_id}: {old_tick_size} -> {new_tick_size}"
        )

        if self.on_tick_size_change:
            await self.on_tick_size_change(token_id, new_tick_size)

    def _parse_levels(self, levels_data: List) -> List[OrderbookLevel]:
        """Parse orderbook levels from message data."""
        levels = []
        for level in levels_data:
            try:
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if price > 0 and size > 0:
                    levels.append(OrderbookLevel(price=price, size=size))
            except (ValueError, TypeError):
                continue
        return levels

    def _update_level(
        self,
        levels: List[OrderbookLevel],
        price: float,
        size: float,
        descending: bool,
    ) -> None:
        """
        Update a level in the orderbook.

        Args:
            levels: List of orderbook levels to update
            price: Price level to update
            size: New size (0 means remove)
            descending: True for bids (descending), False for asks (ascending)
        """
        # Find existing level at this price
        for i, level in enumerate(levels):
            if abs(level.price - price) < 1e-10:
                if size <= 0:
                    # Remove level
                    levels.pop(i)
                else:
                    # Update size
                    levels[i] = OrderbookLevel(price=price, size=size)
                return

        # Level doesn't exist - add if size > 0
        if size > 0:
            new_level = OrderbookLevel(price=price, size=size)
            # Insert in sorted order
            for i, level in enumerate(levels):
                if descending:
                    if price > level.price:
                        levels.insert(i, new_level)
                        return
                else:
                    if price < level.price:
                        levels.insert(i, new_level)
                        return
            levels.append(new_level)

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket."""
        self._running = False
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def add_tokens(self, token_ids: List[str]) -> None:
        """
        Add tokens to the subscription.

        Note: Requires reconnection to take effect.
        """
        for token_id in token_ids:
            self._subscribed_tokens.add(token_id)
            if token_id not in self._orderbooks:
                self._orderbooks[token_id] = OrderbookState(token_id=token_id)

        # Reconnect to update subscription
        if self._running and self._websocket:
            subscription = {"assets_ids": list(self._subscribed_tokens)}
            await self._websocket.send(json.dumps(subscription))
            logger.info(f"Updated subscription to {len(self._subscribed_tokens)} tokens")

    async def remove_tokens(self, token_ids: List[str]) -> None:
        """
        Remove tokens from the subscription.

        Note: Requires reconnection to fully take effect.
        """
        for token_id in token_ids:
            self._subscribed_tokens.discard(token_id)
            self._orderbooks.pop(token_id, None)

        # Update subscription
        if self._running and self._websocket:
            subscription = {"assets_ids": list(self._subscribed_tokens)}
            await self._websocket.send(json.dumps(subscription))
            logger.info(f"Updated subscription to {len(self._subscribed_tokens)} tokens")
