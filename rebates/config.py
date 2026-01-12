"""
Configuration for the 15-minute crypto rebates bot.

Strategy: "Only 50"
- Always place orders at exactly 0.50 on both UP and DOWN sides
- Let orders sit until they fill (no competitive updates)
- When price crosses 50, orders fill automatically
- Only failure mode is execution risk (order doesn't fill when price crosses)
"""
import os

# Trading parameters
TRADE_SIZE = float(os.getenv("REBATES_TRADE_SIZE", "5"))  # $ per side (5 = Polymarket minimum)

# Fixed price - always place at 0.50
# This is the core of the "Only 50" strategy
FIXED_PRICE = 0.50

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
