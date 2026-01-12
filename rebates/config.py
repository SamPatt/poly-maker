"""
Configuration for the 15-minute crypto rebates bot.
"""
import os

# Trading parameters
TRADE_SIZE = float(os.getenv("REBATES_TRADE_SIZE", "5"))  # $ per side (5 = Polymarket minimum)
# Use 0.50 as target - we want fills more than being conservative
# Rebates are still earned at any price, 50% just gives slightly higher rate
TARGET_PRICE = float(os.getenv("REBATES_TARGET_PRICE", "0.50"))

# Initial aggression: how much of the spread to cross toward the ask (0.0-1.0)
# 0.0 = place at bid + 1 tick (conservative, may not fill)
# 0.5 = place at midpoint (good fill rate)
# 0.7 = place 70% toward ask (high fill rate, still maker)
# Goal: maximize fills on BOTH sides at ~0.50 for max rebates
INITIAL_AGGRESSION = float(os.getenv("REBATES_INITIAL_AGGRESSION", "0.50"))

# Timing parameters
SAFETY_BUFFER_SECONDS = int(os.getenv("REBATES_SAFETY_BUFFER", "30"))  # Don't trade if market starts within this time
CHECK_INTERVAL_SECONDS = int(os.getenv("REBATES_CHECK_INTERVAL", "60"))  # How often to check for new markets

# Crypto assets to trade
ASSETS = ["btc", "eth", "sol"]

# Dry run mode - set to false to execute real trades
DRY_RUN = os.getenv("REBATES_DRY_RUN", "true").lower() == "true"

# Position imbalance settings
# Maximum allowed imbalance between Up and Down positions (in shares)
# If abs(up_position - down_position) > MAX_POSITION_IMBALANCE, skip orders on the overweight side
MAX_POSITION_IMBALANCE = float(os.getenv("REBATES_MAX_IMBALANCE", "10"))  # Default: 10 shares (2x trade size)

# API endpoints
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Slug patterns for 15-minute markets
# Format: {asset}-updown-15m-{timestamp}
# Where timestamp is Unix seconds for the END of the 15-minute slot
SLUG_PATTERN = "{asset}-updown-15m-{timestamp}"
