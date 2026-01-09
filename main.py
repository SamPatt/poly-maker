import gc                      # Garbage collection
import os                      # Operating system interface
import time                    # Time functions
import asyncio                 # Asynchronous I/O
import traceback               # Exception handling
import threading               # Thread management
import signal                  # Signal handling
import sys                     # System functions

from poly_data.polymarket_client import PolymarketClient
from poly_data.data_utils import update_markets, update_positions, update_orders
from poly_data.websocket_handlers import connect_market_websocket, connect_user_websocket
import poly_data.global_state as global_state
from poly_data.data_processing import remove_from_performing
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# Balance update interval in seconds (5 minutes)
BALANCE_UPDATE_INTERVAL = 300

# Dry-run mode check
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Try to import Telegram alerts (optional)
try:
    from alerts.telegram import send_startup_alert, send_shutdown_alert, send_error_alert
    TELEGRAM_ENABLED = True
except ImportError:
    TELEGRAM_ENABLED = False
    print("Telegram alerts not available - install alerts module")


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    print(f"\nReceived signal {signum}, shutting down...")
    if TELEGRAM_ENABLED:
        send_shutdown_alert("Received shutdown signal")
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def update_wallet_balance():
    """
    Fetch current USDC wallet balance from blockchain.
    Only updates if enough time has passed since last check to avoid rate limiting.
    """
    try:
        current_time = time.time()

        # Check if we need to update (first time or interval passed)
        if (global_state.last_balance_check is None or
            current_time - global_state.last_balance_check > BALANCE_UPDATE_INTERVAL):

            balance = global_state.client.get_usdc_balance()
            global_state.wallet_balance = balance
            global_state.last_balance_check = current_time

            # Also update committed funds when refreshing balance
            update_committed_funds()

            print(f"Wallet balance updated: ${balance:.2f} USDC, "
                  f"Committed: ${global_state.committed_buy_orders:.2f}, "
                  f"Available: ${balance - global_state.committed_buy_orders:.2f}")
    except Exception as e:
        print(f"Error updating wallet balance: {e}")


def update_committed_funds():
    """
    Calculate total funds committed to open buy orders.
    This is tracked locally to determine available balance for new orders.
    """
    try:
        total_committed = 0.0

        # Sum up all open buy orders
        for token_id, order_data in global_state.orders.items():
            buy_order = order_data.get('buy', {})
            price = buy_order.get('price', 0)
            size = buy_order.get('size', 0)

            # Committed amount = price * size (what we'd pay if filled)
            if price > 0 and size > 0:
                total_committed += price * size

        global_state.committed_buy_orders = total_committed
    except Exception as e:
        print(f"Error calculating committed funds: {e}")


def get_available_balance():
    """
    Get available balance for new orders (wallet balance - committed funds).
    """
    return global_state.wallet_balance - global_state.committed_buy_orders


def update_once():
    """
    Initialize the application state by fetching market data, positions, and orders.
    """
    update_markets()    # Get market information from Google Sheets
    update_positions()  # Get current positions from Polymarket
    update_orders()     # Get current orders from Polymarket

    # Fetch initial wallet balance
    update_wallet_balance()

def remove_from_pending():
    """
    Clean up stale trades that have been pending for too long (>15 seconds).
    This prevents the system from getting stuck on trades that may have failed.
    """
    try:
        current_time = time.time()
            
        # Iterate through all performing trades
        for col in list(global_state.performing.keys()):
            for trade_id in list(global_state.performing[col]):
                
                try:
                    # If trade has been pending for more than 15 seconds, remove it
                    if current_time - global_state.performing_timestamps[col].get(trade_id, current_time) > 15:
                        print(f"Removing stale entry {trade_id} from {col} after 15 seconds")
                        remove_from_performing(col, trade_id)
                        print("After removing: ", global_state.performing, global_state.performing_timestamps)
                except:
                    print("Error in remove_from_pending")
                    print(traceback.format_exc())                
    except:
        print("Error in remove_from_pending")
        print(traceback.format_exc())

def update_periodically():
    """
    Background thread function that periodically updates market data, positions and orders.
    - Positions and orders are updated every 5 seconds
    - Market data is updated every 30 seconds (every 6 cycles)
    - Stale pending trades are removed each cycle
    """
    i = 1
    while True:
        time.sleep(5)  # Update every 5 seconds
        
        try:
            # Clean up stale trades
            remove_from_pending()
            
            # Update positions and orders every cycle
            update_positions(avgOnly=True)  # Only update average price, not position size
            update_orders()

            # Update committed funds after orders refresh (fast, local calculation)
            update_committed_funds()

            # Update wallet balance periodically (blockchain call, rate-limited internally)
            update_wallet_balance()

            # Update market data every 6th cycle (30 seconds)
            if i % 6 == 0:
                update_markets()
                i = 1
                    
            gc.collect()  # Force garbage collection to free memory
            i += 1
        except:
            print("Error in update_periodically")
            print(traceback.format_exc())
            
async def main():
    """
    Main application entry point. Initializes client, data, and manages websocket connections.
    """
    # Display startup banner
    print("=" * 60)
    print("POLY-MAKER - Polymarket Market Making Bot")
    print("=" * 60)

    if DRY_RUN:
        print("")
        print("*" * 60)
        print("*  DRY RUN MODE ENABLED                                    *")
        print("*  No real orders will be placed                           *")
        print("*  Set DRY_RUN=false in .env to enable live trading        *")
        print("*" * 60)
        print("")

    # Send startup alert via Telegram
    if TELEGRAM_ENABLED:
        send_startup_alert(dry_run=DRY_RUN)

    # Initialize client
    global_state.client = PolymarketClient()

    # Initialize state and fetch initial data
    global_state.all_tokens = []
    update_once()
    print("After initial updates: ", global_state.orders, global_state.positions)

    print("\n")
    print(f'There are {len(global_state.df)} market, {len(global_state.positions)} positions and {len(global_state.orders)} orders. Starting positions: {global_state.positions}')

    # Start background update thread
    update_thread = threading.Thread(target=update_periodically, daemon=True)
    update_thread.start()
    
    # Main loop - maintain websocket connections
    while True:
        try:
            # Connect to market and user websockets simultaneously
            await asyncio.gather(
                connect_market_websocket(global_state.all_tokens), 
                connect_user_websocket()
            )
            print("Reconnecting to the websocket")
        except:
            print("Error in main loop")
            print(traceback.format_exc())
            
        await asyncio.sleep(1)
        gc.collect()  # Clean up memory

if __name__ == "__main__":
    asyncio.run(main())