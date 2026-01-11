"""
Sample market data fixtures for testing.

Provides realistic test data for:
- Orderbook data (bids/asks)
- Market configuration rows
- Position data
- Order data
"""

from sortedcontainers import SortedDict
from typing import Dict, Any


def create_orderbook(
    bids: Dict[float, float] = None, asks: Dict[float, float] = None
) -> Dict[str, SortedDict]:
    """
    Create a mock orderbook with bids and asks.

    Args:
        bids: Dict of price -> size for bids (default provides sample data)
        asks: Dict of price -> size for asks (default provides sample data)

    Returns:
        Dict with 'bids' and 'asks' as SortedDicts
    """
    if bids is None:
        bids = {
            0.45: 100.0,
            0.44: 200.0,
            0.43: 300.0,
            0.42: 150.0,
            0.41: 500.0,
        }
    if asks is None:
        asks = {
            0.55: 100.0,
            0.56: 200.0,
            0.57: 300.0,
            0.58: 150.0,
            0.59: 500.0,
        }

    return {
        "bids": SortedDict(bids),
        "asks": SortedDict(asks),
    }


def create_empty_orderbook() -> Dict[str, SortedDict]:
    """Create an empty orderbook."""
    return {
        "bids": SortedDict(),
        "asks": SortedDict(),
    }


def create_thin_orderbook() -> Dict[str, SortedDict]:
    """Create a thin orderbook with minimal liquidity."""
    return {
        "bids": SortedDict({0.40: 5.0}),
        "asks": SortedDict({0.60: 5.0}),
    }


def create_market_row(
    token1: str = "token1_abc123",
    token2: str = "token2_xyz789",
    trade_size: float = 100.0,
    max_size: float = 500.0,
    min_size: float = 10.0,
    tick_size: float = 0.01,
    max_spread: float = 5.0,
    neg_risk: str = "FALSE",
    question: str = "Will BTC be above $100k?",
    outcome1: str = "Yes",
    outcome2: str = "No",
    market_id: str = "market_123",
    **kwargs
) -> Dict[str, Any]:
    """
    Create a mock market configuration row.

    Args:
        token1: Token ID for outcome 1 (typically YES)
        token2: Token ID for outcome 2 (typically NO)
        trade_size: Default trade size in USDC
        max_size: Maximum position size
        min_size: Minimum order size
        tick_size: Price tick size
        max_spread: Maximum spread for incentive calculation
        neg_risk: "TRUE" or "FALSE" for negative risk markets
        question: Market question text
        outcome1: Name of outcome 1
        outcome2: Name of outcome 2
        market_id: Market condition ID
        **kwargs: Additional fields to include

    Returns:
        Dict representing a market configuration row
    """
    row = {
        "token1": token1,
        "token2": token2,
        "trade_size": trade_size,
        "max_size": max_size,
        "min_size": min_size,
        "tick_size": tick_size,
        "max_spread": max_spread,
        "neg_risk": neg_risk,
        "question": question,
        "outcome1": outcome1,
        "outcome2": outcome2,
        "market_id": market_id,
        "type": "default",
        "enabled": "TRUE",
    }
    row.update(kwargs)
    return row


def create_neg_risk_market_row(**kwargs) -> Dict[str, Any]:
    """Create a market row for a negative risk market."""
    defaults = {
        "neg_risk": "TRUE",
        "question": "US Election: Will Trump win?",
    }
    defaults.update(kwargs)
    return create_market_row(**defaults)


def create_position(size: float = 100.0, avg_price: float = 0.50) -> Dict[str, float]:
    """
    Create a mock position.

    Args:
        size: Position size in tokens
        avg_price: Average entry price

    Returns:
        Dict with 'size' and 'avgPrice'
    """
    return {"size": size, "avgPrice": avg_price}


def create_order(price: float = 0.0, size: float = 0.0) -> Dict[str, float]:
    """
    Create a mock order.

    Args:
        price: Order price
        size: Order size

    Returns:
        Dict with 'price' and 'size'
    """
    return {"price": price, "size": size}


def create_order_pair(
    buy_price: float = 0.0,
    buy_size: float = 0.0,
    sell_price: float = 0.0,
    sell_size: float = 0.0,
) -> Dict[str, Dict[str, float]]:
    """
    Create a buy/sell order pair for a token.

    Args:
        buy_price: Buy order price
        buy_size: Buy order size
        sell_price: Sell order price
        sell_size: Sell order size

    Returns:
        Dict with 'buy' and 'sell' orders
    """
    return {
        "buy": create_order(buy_price, buy_size),
        "sell": create_order(sell_price, sell_size),
    }


# Sample hyperparameters by market type
SAMPLE_PARAMS = {
    "default": {
        "stop_loss_threshold": -5.0,
        "take_profit_threshold": 3.0,
        "volatility_threshold": 15.0,
        "spread_threshold": 0.05,
        "sleep_period": 6,
    },
    "crypto": {
        "stop_loss_threshold": -8.0,
        "take_profit_threshold": 5.0,
        "volatility_threshold": 20.0,
        "spread_threshold": 0.08,
        "sleep_period": 4,
    },
    "election": {
        "stop_loss_threshold": -3.0,
        "take_profit_threshold": 2.0,
        "volatility_threshold": 10.0,
        "spread_threshold": 0.03,
        "sleep_period": 12,
    },
}


# Sample token IDs for testing
SAMPLE_TOKENS = {
    "token1": "71321045679013447797864514828570804964740509273960966121330942155432910325412",
    "token2": "48331043485689647100567840212356610899562973377511805061777607348787526892111",
}


# Sample market IDs
SAMPLE_MARKETS = {
    "btc_100k": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    "eth_5k": "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
}
