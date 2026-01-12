"""
Configuration for Gabagool arbitrage strategy.

Fee structure (15-minute crypto markets):
- Maker orders: FREE (no fees, plus earn rebates!)
- Taker orders: Up to 1.56% at 50% odds
- Winner fee: NONE (no fee on profits!)
- Gas: ~$0.002 per merge transaction
"""

import os

from .circuit_breaker import CircuitBreakerConfig


# ============== PROFIT THRESHOLDS ==============

# Maximum combined cost (YES + NO) to consider profitable
# Lower = more conservative, higher = more opportunities
#
# With NO winner fee, thresholds are much tighter:
# - Maker only: 0.99 (1% spread, ~$1 profit per $100)
# - Taker rescue: 0.98 (2% spread to cover 1.56% taker fee)
# - Conservative: 0.985 (1.5% spread, good balance)
PROFIT_THRESHOLD = float(os.getenv("GABAGOOL_PROFIT_THRESHOLD", "0.99"))

# Minimum net profit percentage to execute
# With no winner fee, even 0.5% is meaningful profit
MIN_NET_PROFIT_PCT = float(os.getenv("GABAGOOL_MIN_NET_PROFIT", "0.5"))


# ============== POSITION SIZING ==============

# Trade size per opportunity (USDC)
TRADE_SIZE = float(os.getenv("GABAGOOL_TRADE_SIZE", "50"))

# Maximum position per market (USDC)
MAX_POSITION_PER_MARKET = float(os.getenv("GABAGOOL_MAX_POSITION", "200"))

# Minimum order size (Polymarket minimum is 5)
MIN_ORDER_SIZE = float(os.getenv("GABAGOOL_MIN_ORDER", "10"))


# ============== EXECUTION ==============

# Order type preference: "MAKER", "TAKER", or "HYBRID"
EXECUTION_MODE = os.getenv("GABAGOOL_EXECUTION_MODE", "HYBRID")

# Aggression for maker orders (0.0 = conservative, 1.0 = aggressive)
MAKER_AGGRESSION = float(os.getenv("GABAGOOL_MAKER_AGGRESSION", "0.5"))

# Maximum price to pay on taker orders
MAX_TAKER_PRICE = float(os.getenv("GABAGOOL_MAX_TAKER_PRICE", "0.55"))


# ============== TIMING ==============

# Minimum seconds before market start to enter
MIN_TIME_TO_START = int(os.getenv("GABAGOOL_MIN_TIME", "60"))

# Time threshold to switch from maker to taker
TAKER_SWITCH_TIME = int(os.getenv("GABAGOOL_TAKER_SWITCH", "30"))

# Maximum time to wait for fills (seconds)
MAX_FILL_WAIT = int(os.getenv("GABAGOOL_MAX_FILL_WAIT", "45"))

# Scan interval for opportunities (seconds)
SCAN_INTERVAL = float(os.getenv("GABAGOOL_SCAN_INTERVAL", "1.0"))


# ============== RISK MANAGEMENT ==============

# Maximum number of concurrent positions
MAX_CONCURRENT_POSITIONS = int(os.getenv("GABAGOOL_MAX_CONCURRENT", "5"))

# Maximum imbalance tolerance before emergency exit
MAX_IMBALANCE_PCT = float(os.getenv("GABAGOOL_MAX_IMBALANCE", "20"))

# Minimum liquidity required (shares)
MIN_LIQUIDITY = float(os.getenv("GABAGOOL_MIN_LIQUIDITY", "50"))

# Maximum gas cost (USD) to allow execution
MAX_GAS_COST = float(os.getenv("GABAGOOL_MAX_GAS", "0.50"))


# ============== CIRCUIT BREAKER ==============

# Position limits
CB_MAX_POSITION_PER_MARKET = float(os.getenv("GABAGOOL_CB_MAX_POS_MARKET", "500"))
CB_MAX_TOTAL_POSITION = float(os.getenv("GABAGOOL_CB_MAX_POS_TOTAL", "2000"))

# Loss limits
CB_MAX_DAILY_LOSS = float(os.getenv("GABAGOOL_CB_MAX_DAILY_LOSS", "100"))
CB_MAX_LOSS_PER_TRADE = float(os.getenv("GABAGOOL_CB_MAX_LOSS_TRADE", "20"))

# Error limits
CB_MAX_CONSECUTIVE_ERRORS = int(os.getenv("GABAGOOL_CB_MAX_CONSEC_ERRORS", "5"))
CB_MAX_ERRORS_PER_HOUR = int(os.getenv("GABAGOOL_CB_MAX_ERRORS_HOUR", "20"))

# Timing
CB_COOLDOWN_SECONDS = int(os.getenv("GABAGOOL_CB_COOLDOWN", "300"))

# Recovery
CB_AUTO_RECOVER = os.getenv("GABAGOOL_CB_AUTO_RECOVER", "true").lower() == "true"
CB_REQUIRE_MANUAL_RESET = (
    os.getenv("GABAGOOL_CB_MANUAL_RESET", "false").lower() == "true"
)


# ============== POSITION RECONCILIATION ==============

# Delay before auto-closing excess positions (seconds)
RECONCILIATION_DELAY = float(os.getenv("GABAGOOL_RECONCILE_DELAY", "2.0"))

# Minimum imbalance to trigger reconciliation (shares)
RECONCILIATION_MIN_IMBALANCE = float(os.getenv("GABAGOOL_RECONCILE_MIN", "1.0"))


# ============== MODE ==============

# Enable Gabagool strategy (can run alongside rebates)
ENABLED = os.getenv("GABAGOOL_ENABLED", "true").lower() == "true"

# Dry run mode (no real trades)
DRY_RUN = os.getenv("GABAGOOL_DRY_RUN", "true").lower() == "true"


# ============== API ==============

# Polymarket CLOB API endpoint
CLOB_API_BASE = "https://clob.polymarket.com"


def get_circuit_breaker_config() -> CircuitBreakerConfig:
    """Build CircuitBreakerConfig from environment variables."""
    return CircuitBreakerConfig(
        max_position_per_market=CB_MAX_POSITION_PER_MARKET,
        max_total_position=CB_MAX_TOTAL_POSITION,
        max_daily_loss=CB_MAX_DAILY_LOSS,
        max_loss_per_trade=CB_MAX_LOSS_PER_TRADE,
        max_consecutive_errors=CB_MAX_CONSECUTIVE_ERRORS,
        max_errors_per_hour=CB_MAX_ERRORS_PER_HOUR,
        cooldown_seconds=CB_COOLDOWN_SECONDS,
        auto_recover=CB_AUTO_RECOVER,
        require_manual_reset=CB_REQUIRE_MANUAL_RESET,
    )
