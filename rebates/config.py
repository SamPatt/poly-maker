"""
Configuration for the 15-minute crypto rebates bot.
"""
import os

# Trading parameters
TRADE_SIZE = float(os.getenv("REBATES_TRADE_SIZE", "5"))  # $ per side (5 = Polymarket minimum)
# Use 0.49 instead of 0.50 to avoid crossing the book when one side is imbalanced
# Rebates are still earned at any price, 50% just gives slightly higher rate
TARGET_PRICE = float(os.getenv("REBATES_TARGET_PRICE", "0.49"))

# Timing parameters
SAFETY_BUFFER_SECONDS = int(os.getenv("REBATES_SAFETY_BUFFER", "30"))  # Don't trade if market starts within this time
CHECK_INTERVAL_SECONDS = int(os.getenv("REBATES_CHECK_INTERVAL", "60"))  # How often to check for new markets

# Crypto assets to trade
ASSETS = ["btc", "eth", "sol"]

# Dry run mode - set to false to execute real trades
DRY_RUN = os.getenv("REBATES_DRY_RUN", "true").lower() == "true"

# API endpoints
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Slug patterns for 15-minute markets
# Format: {asset}-updown-15m-{timestamp}
# Where timestamp is Unix seconds for the END of the 15-minute slot
SLUG_PATTERN = "{asset}-updown-15m-{timestamp}"
