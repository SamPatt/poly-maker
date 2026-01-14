"""
Data models for Active Two-Sided Quoting Strategy.

Contains all data classes used by the active quoting system.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, List


class OrderSide(Enum):
    """Order side (buy/sell)."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Order status from exchange."""
    PENDING = "PENDING"  # Submitted but not confirmed
    OPEN = "OPEN"  # Active on orderbook
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # Some quantity filled
    FILLED = "FILLED"  # Fully filled
    CANCELLED = "CANCELLED"  # Cancelled by user or system
    EXPIRED = "EXPIRED"  # Time-to-live expired
    REJECTED = "REJECTED"  # Rejected by exchange


@dataclass
class Quote:
    """A two-sided quote with bid and ask prices and sizes."""
    token_id: str
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def spread(self) -> float:
        """Calculate the spread between ask and bid."""
        return self.ask_price - self.bid_price

    def spread_ticks(self, tick_size: float) -> int:
        """Calculate the spread in ticks."""
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        return int(round(self.spread() / tick_size))

    def mid_price(self) -> float:
        """Calculate the mid price."""
        return (self.bid_price + self.ask_price) / 2

    def is_valid(self) -> bool:
        """Check if quote is valid (bid < ask, positive sizes)."""
        return (
            self.bid_price < self.ask_price
            and self.bid_price > 0
            and self.ask_price < 1.0
            and self.bid_size > 0
            and self.ask_size > 0
        )


@dataclass
class OrderbookLevel:
    """A single level in the orderbook."""
    price: float
    size: float

    def __post_init__(self):
        if self.price < 0:
            raise ValueError("price must be non-negative")
        if self.size < 0:
            raise ValueError("size must be non-negative")


@dataclass
class OrderbookState:
    """Real-time orderbook state for a single token."""
    token_id: str
    bids: List[OrderbookLevel] = field(default_factory=list)  # Sorted by price descending
    asks: List[OrderbookLevel] = field(default_factory=list)  # Sorted by price ascending
    tick_size: float = 0.01
    last_trade_price: Optional[float] = None
    last_update_time: datetime = field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> Optional[float]:
        """Get the best bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Get the best ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> Optional[float]:
        """Get the best bid size."""
        return self.bids[0].size if self.bids else None

    @property
    def best_ask_size(self) -> Optional[float]:
        """Get the best ask size."""
        return self.asks[0].size if self.asks else None

    def spread(self) -> Optional[float]:
        """Calculate the spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def spread_ticks(self) -> Optional[int]:
        """Calculate the spread in ticks."""
        spread = self.spread()
        if spread is not None and self.tick_size > 0:
            return int(round(spread / self.tick_size))
        return None

    def mid_price(self) -> Optional[float]:
        """Calculate the mid price."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def bid_depth(self, levels: int = 5) -> float:
        """Calculate total bid depth for the first n levels."""
        return sum(level.size for level in self.bids[:levels])

    def ask_depth(self, levels: int = 5) -> float:
        """Calculate total ask depth for the first n levels."""
        return sum(level.size for level in self.asks[:levels])

    def is_valid(self) -> bool:
        """Check if orderbook is valid (has both sides, not crossed)."""
        if not self.bids or not self.asks:
            return False
        return self.best_bid < self.best_ask


@dataclass
class MomentumState:
    """Momentum detection state for adverse selection protection."""
    token_id: str
    is_active: bool = False  # True if in cooldown
    cooldown_until: Optional[datetime] = None
    last_trade_prices: List[float] = field(default_factory=list)  # Recent trade prices
    last_trade_times: List[datetime] = field(default_factory=list)  # Timestamps of recent trades
    last_bid_depth: Optional[float] = None  # For sweep detection
    last_ask_depth: Optional[float] = None

    def in_cooldown(self) -> bool:
        """Check if currently in cooldown period."""
        if not self.is_active or self.cooldown_until is None:
            return False
        return datetime.utcnow() < self.cooldown_until

    def add_trade(self, price: float, timestamp: Optional[datetime] = None) -> None:
        """Add a trade to the history."""
        ts = timestamp or datetime.utcnow()
        self.last_trade_prices.append(price)
        self.last_trade_times.append(ts)
        # Keep only last 100 trades
        if len(self.last_trade_prices) > 100:
            self.last_trade_prices = self.last_trade_prices[-100:]
            self.last_trade_times = self.last_trade_times[-100:]

    def price_change_ticks(self, window_ms: int, tick_size: float) -> int:
        """Calculate price change in ticks within the time window."""
        if len(self.last_trade_prices) < 2:
            return 0

        now = datetime.utcnow()
        cutoff = now.timestamp() - (window_ms / 1000.0)

        # Get prices within window
        prices_in_window = []
        for price, ts in zip(self.last_trade_prices, self.last_trade_times):
            if ts.timestamp() >= cutoff:
                prices_in_window.append(price)

        if len(prices_in_window) < 2:
            return 0

        price_change = abs(prices_in_window[-1] - prices_in_window[0])
        return int(round(price_change / tick_size))


@dataclass
class Fill:
    """A fill (executed trade) from the user channel."""
    order_id: str
    token_id: str
    side: OrderSide
    price: float
    size: float
    fee: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    trade_id: Optional[str] = None

    @property
    def notional(self) -> float:
        """Calculate the notional value of the fill."""
        return self.price * self.size

    @property
    def net_cost(self) -> float:
        """Calculate the net cost including fees."""
        if self.side == OrderSide.BUY:
            return self.notional + self.fee
        else:
            return -self.notional + self.fee


@dataclass
class OrderState:
    """State of a single order."""
    order_id: str
    token_id: str
    side: OrderSide
    price: float
    original_size: float
    remaining_size: float
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    fills: List[Fill] = field(default_factory=list)
    post_only: bool = True
    fee_rate_bps: int = 0

    @property
    def filled_size(self) -> float:
        """Calculate the filled size."""
        return self.original_size - self.remaining_size

    @property
    def fill_percentage(self) -> float:
        """Calculate the fill percentage."""
        if self.original_size == 0:
            return 0.0
        return (self.filled_size / self.original_size) * 100

    def is_open(self) -> bool:
        """Check if order is still active."""
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    def is_done(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)


@dataclass
class Position:
    """Position state for a single token."""
    token_id: str
    size: float = 0.0  # Positive = long, negative = short (though shorts not supported)
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0  # From closed trades
    unrealized_pnl: float = 0.0  # Based on current mark price
    total_fees_paid: float = 0.0

    @property
    def notional(self) -> float:
        """Calculate the notional value of the position."""
        return abs(self.size) * self.avg_entry_price

    @property
    def max_liability(self) -> float:
        """
        Calculate the maximum liability (worst-case loss).
        For binary options, max loss = entry price per share (if goes to 0).
        """
        return abs(self.size) * self.avg_entry_price

    def update_from_fill(self, fill: Fill) -> None:
        """Update position based on a fill."""
        if fill.side == OrderSide.BUY:
            # Buying increases position
            if self.size == 0:
                self.avg_entry_price = fill.price
                self.size = fill.size
            else:
                # Weighted average entry price
                total_cost = (self.size * self.avg_entry_price) + (fill.size * fill.price)
                self.size += fill.size
                if self.size > 0:
                    self.avg_entry_price = total_cost / self.size
        else:
            # Selling decreases position
            if self.size > 0:
                # Calculate realized PnL
                pnl_per_share = fill.price - self.avg_entry_price
                shares_sold = min(fill.size, self.size)
                self.realized_pnl += pnl_per_share * shares_sold
            self.size -= fill.size
            # If position flipped to negative (shouldn't happen in this system), reset avg price
            if self.size < 0:
                self.avg_entry_price = fill.price

        self.total_fees_paid += fill.fee


@dataclass
class MarketState:
    """Combined state for a single market (token pair)."""
    token_id: str
    reverse_token_id: str  # The other side of the binary
    asset: str  # e.g., "BTC", "ETH", "SOL"
    orderbook: OrderbookState
    momentum: MomentumState
    position: Position
    condition_id: str = ""  # Market's condition ID for redemption
    open_orders: Dict[str, OrderState] = field(default_factory=dict)  # order_id -> OrderState
    is_quoting: bool = False  # Whether we have active quotes
    last_quote: Optional[Quote] = None
    drawdown_usdc: float = 0.0  # Cumulative loss for this market

    def total_open_order_size(self, side: OrderSide) -> float:
        """Calculate total open order size for a side."""
        return sum(
            order.remaining_size
            for order in self.open_orders.values()
            if order.side == side and order.is_open()
        )

    def should_stop_quoting(self, max_drawdown: float) -> bool:
        """Check if we should stop quoting this market due to drawdown."""
        return self.drawdown_usdc >= max_drawdown
