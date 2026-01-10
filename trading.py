import gc                       # Garbage collection
import os                       # Operating system interface
import json                     # JSON handling
import asyncio                  # Asynchronous I/O
import traceback                # Exception handling
import pandas as pd             # Data analysis library
import math                     # Mathematical functions
from dotenv import load_dotenv

import poly_data.global_state as global_state
import poly_data.CONSTANTS as CONSTANTS

# Import utility functions for trading
from poly_data.trading_utils import get_best_bid_ask_deets, get_order_prices, get_buy_sell_amount, round_down, round_up
from poly_data.data_utils import get_position, get_order, set_position

# Load environment variables
load_dotenv()

# Dry-run mode: log orders instead of executing them
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

if DRY_RUN:
    print("=" * 50)
    print("DRY RUN MODE ENABLED - No real orders will be placed")
    print("=" * 50)

# Try to import Telegram alerts (optional)
try:
    from alerts.telegram import send_trade_alert, send_stop_loss_alert, send_error_alert
    TELEGRAM_ENABLED = True
except ImportError:
    TELEGRAM_ENABLED = False
    print("Telegram alerts not available")

# Try to import database for trade recording (optional)
try:
    from db.supabase_client import record_trade
    DB_ENABLED = True
except ImportError:
    DB_ENABLED = False

# Create directory for storing position risk information
if not os.path.exists('positions/'):
    os.makedirs('positions/')

# Track markets that have been attempted for exit (to avoid repeated attempts)
_exit_attempted_markets = set()

def send_buy_order(order):
    """
    Create a BUY order for a specific token.

    This function:
    1. Checks if wallet has sufficient available balance
    2. Cancels any existing orders for the token if needed
    3. Checks if the order price is within acceptable range
    4. Creates a new buy order if conditions are met
    5. Updates local order state immediately to prevent duplicates

    Args:
        order (dict): Order details including token, price, size, and market parameters
    """
    client = global_state.client
    token = order['token']

    # Check available balance before placing order
    order_cost = order['price'] * order['size']
    available_balance = global_state.wallet_balance - global_state.committed_buy_orders

    if available_balance < order_cost + global_state.MIN_AVAILABLE_BALANCE:
        print(f"[BALANCE CHECK] Insufficient funds for buy order. "
              f"Available: ${available_balance:.2f}, Order cost: ${order_cost:.2f}, "
              f"Min reserve: ${global_state.MIN_AVAILABLE_BALANCE:.2f}")
        return

    # Only cancel existing orders if we need to make significant changes
    existing_buy_size = order['orders']['buy']['size']
    existing_buy_price = order['orders']['buy']['price']

    # Cancel orders if price changed significantly or size needs major adjustment
    price_diff = abs(existing_buy_price - order['price']) if existing_buy_price > 0 else float('inf')
    size_diff = abs(existing_buy_size - order['size']) if existing_buy_size > 0 else float('inf')

    should_cancel = (
        price_diff > 0.005 or  # Cancel if price diff > 0.5 cents
        size_diff > order['size'] * 0.1 or  # Cancel if size diff > 10%
        existing_buy_size == 0  # Cancel if no existing buy order
    )

    if should_cancel and (existing_buy_size > 0 or order['orders']['sell']['size'] > 0):
        print(f"Cancelling buy orders - price diff: {price_diff:.4f}, size diff: {size_diff:.1f}")
        if DRY_RUN:
            print(f"[DRY RUN] Would cancel orders for {order['token']}")
        else:
            client.cancel_all_asset(order['token'])
            # Clear local order state after cancellation
            if str(token) in global_state.orders:
                global_state.orders[str(token)]['buy'] = {'price': 0, 'size': 0}
                global_state.orders[str(token)]['sell'] = {'price': 0, 'size': 0}
    elif not should_cancel:
        print(f"Keeping existing buy orders - minor changes: price diff: {price_diff:.4f}, size diff: {size_diff:.1f}")
        return  # Don't place new order if existing one is fine

    # Calculate minimum acceptable price based on market spread
    incentive_start = order['mid_price'] - order['max_spread']/100

    trade = True

    # Don't place orders that are below incentive threshold
    if order['price'] < incentive_start:
        trade = False

    if trade:
        # Only place orders with prices between 0.1 and 0.9 to avoid extreme positions
        if order['price'] >= 0.1 and order['price'] < 0.9:
            print(f'Creating new order for {order["size"]} at {order["price"]}')
            print(order['token'], 'BUY', order['price'], order['size'])

            if DRY_RUN:
                print(f"[DRY RUN] Would create BUY order: {order['token']} @ {order['price']} x {order['size']}")
                # Track committed funds even in dry-run mode for testing
                global_state.committed_buy_orders += order['price'] * order['size']
            else:
                client.create_order(
                    order['token'],
                    'BUY',
                    order['price'],
                    order['size'],
                    True if order['neg_risk'] == 'TRUE' else False
                )
                # Track committed funds immediately to prevent over-ordering
                global_state.committed_buy_orders += order['price'] * order['size']

                # CRITICAL: Update local order state immediately to prevent duplicate orders
                # This prevents the race condition where the next loop runs before websocket updates
                token_str = str(order['token'])
                if token_str not in global_state.orders:
                    global_state.orders[token_str] = {'buy': {'price': 0, 'size': 0}, 'sell': {'price': 0, 'size': 0}}
                global_state.orders[token_str]['buy'] = {
                    'price': order['price'],
                    'size': order['size']
                }

                # Send Telegram alert and record trade
                if TELEGRAM_ENABLED:
                    row = order.get('row', {}) if isinstance(order.get('row'), dict) else {}
                    market_question = row.get('question')
                    # Determine outcome name based on which token is being traded
                    outcome = None
                    if row.get('token1') and str(order['token']) == str(row['token1']):
                        outcome = row.get('answer1', 'Yes')
                    elif row.get('token2') and str(order['token']) == str(row['token2']):
                        outcome = row.get('answer2', 'No')
                    send_trade_alert('BUY', order['token'], order['price'], order['size'], market_question, outcome)
                if DB_ENABLED:
                    market_question = order.get('row', {}).get('question') if isinstance(order.get('row'), dict) else None
                    record_trade(order['token'], 'BUY', order['price'], order['size'], market_question)
        else:
            print("Not creating buy order because its outside acceptable price range (0.1-0.9)")
    else:
        print(f'Not creating new order because order price of {order["price"]} is less than incentive start price of {incentive_start}. Mid price is {order["mid_price"]}')


def send_sell_order(order):
    """
    Create a SELL order for a specific token.

    This function:
    1. Cancels any existing orders for the token if needed
    2. Creates a new sell order with the specified parameters
    3. Updates local order state immediately to prevent duplicates

    Args:
        order (dict): Order details including token, price, size, and market parameters
    """
    client = global_state.client
    token = order['token']

    # Only cancel existing orders if we need to make significant changes
    existing_sell_size = order['orders']['sell']['size']
    existing_sell_price = order['orders']['sell']['price']

    # Cancel orders if price changed significantly or size needs major adjustment
    price_diff = abs(existing_sell_price - order['price']) if existing_sell_price > 0 else float('inf')
    size_diff = abs(existing_sell_size - order['size']) if existing_sell_size > 0 else float('inf')

    should_cancel = (
        price_diff > 0.005 or  # Cancel if price diff > 0.5 cents
        size_diff > order['size'] * 0.1 or  # Cancel if size diff > 10%
        existing_sell_size == 0  # Cancel if no existing sell order
    )

    if should_cancel and (existing_sell_size > 0 or order['orders']['buy']['size'] > 0):
        print(f"Cancelling sell orders - price diff: {price_diff:.4f}, size diff: {size_diff:.1f}")
        if DRY_RUN:
            print(f"[DRY RUN] Would cancel orders for {order['token']}")
        else:
            client.cancel_all_asset(order['token'])
            # Clear local order state after cancellation
            if str(token) in global_state.orders:
                global_state.orders[str(token)]['buy'] = {'price': 0, 'size': 0}
                global_state.orders[str(token)]['sell'] = {'price': 0, 'size': 0}
    elif not should_cancel:
        print(f"Keeping existing sell orders - minor changes: price diff: {price_diff:.4f}, size diff: {size_diff:.1f}")
        return  # Don't place new order if existing one is fine

    print(f'Creating new order for {order["size"]} at {order["price"]}')

    if DRY_RUN:
        print(f"[DRY RUN] Would create SELL order: {order['token']} @ {order['price']} x {order['size']}")
    else:
        client.create_order(
            order['token'],
            'SELL',
            order['price'],
            order['size'],
            True if order['neg_risk'] == 'TRUE' else False
        )

        # CRITICAL: Update local order state immediately to prevent duplicate orders
        token_str = str(order['token'])
        if token_str not in global_state.orders:
            global_state.orders[token_str] = {'buy': {'price': 0, 'size': 0}, 'sell': {'price': 0, 'size': 0}}
        global_state.orders[token_str]['sell'] = {
            'price': order['price'],
            'size': order['size']
        }

        # Send Telegram alert and record trade
        if TELEGRAM_ENABLED:
            row = order.get('row', {}) if isinstance(order.get('row'), dict) else {}
            market_question = row.get('question')
            # Determine outcome name based on which token is being traded
            outcome = None
            if row.get('token1') and str(order['token']) == str(row['token1']):
                outcome = row.get('answer1', 'Yes')
            elif row.get('token2') and str(order['token']) == str(row['token2']):
                outcome = row.get('answer2', 'No')
            send_trade_alert('SELL', order['token'], order['price'], order['size'], market_question, outcome)
        if DB_ENABLED:
            market_question = order.get('row', {}).get('question') if isinstance(order.get('row'), dict) else None
            record_trade(order['token'], 'SELL', order['price'], order['size'], market_question)

# Dictionary to store locks for each market to prevent concurrent trading on the same market
market_locks = {}

async def perform_trade(market):
    """
    Main trading function that handles market making for a specific market.
    
    This function:
    1. Merges positions when possible to free up capital
    2. Analyzes the market to determine optimal bid/ask prices
    3. Manages buy and sell orders based on position size and market conditions
    4. Implements risk management with stop-loss and take-profit logic
    
    Args:
        market (str): The market ID to trade on
    """
    # Create a lock for this market if it doesn't exist
    if market not in market_locks:
        market_locks[market] = asyncio.Lock()

    # Use lock to prevent concurrent trading on the same market
    async with market_locks[market]:
        try:
            client = global_state.client
            # Get market details from the configuration
            row = global_state.df[global_state.df['condition_id'] == market].iloc[0]      
            # Determine decimal precision from tick size
            round_length = len(str(row['tick_size']).split(".")[1])

            # Get trading parameters for this market type
            params = global_state.params[row['param_type']]
            
            # Create a list with both outcomes for the market
            deets = [
                {'name': 'token1', 'token': row['token1'], 'answer': row['answer1']}, 
                {'name': 'token2', 'token': row['token2'], 'answer': row['answer2']}
            ]
            print(f"\n\n{pd.Timestamp.utcnow().tz_localize(None)}: {row['question']}")

            # Get current positions for both outcomes
            pos_1 = get_position(row['token1'])['size']
            pos_2 = get_position(row['token2'])['size']

            # ------- EXIT BEFORE EVENT LOGIC -------
            # If exit_before_event is enabled and we're within 1 day of event date,
            # cancel orders and sell any one-sided positions
            exit_before = row.get('exit_before_event')
            if exit_before == True or exit_before == 'TRUE' or exit_before == 'true':
                event_date = pd.to_datetime(row.get('event_date'), errors='coerce')
                if pd.notna(event_date):
                    now = pd.Timestamp.now(tz='UTC').tz_localize(None)
                    # Ensure event_date is also tz-naive for comparison
                    if event_date.tzinfo is not None:
                        event_date = event_date.tz_localize(None)
                    time_to_event = event_date - now

                    if time_to_event <= pd.Timedelta(days=1):
                        # Check if we've already attempted to exit this market
                        if market in _exit_attempted_markets:
                            print(f"[EXIT BEFORE EVENT] Already attempted exit for {row['question'][:50]}... skipping")
                            return

                        print(f"[EXIT BEFORE EVENT] Within 1 day of event ({event_date.date()})")

                        # Cancel all open orders first
                        try:
                            client.cancel_all_market(market)
                        except Exception as e:
                            print(f"  Error cancelling orders: {e}")

                        # Check if position is one-sided
                        exit_success = False
                        if pos_1 > 0 and pos_2 == 0:
                            # One-sided: only holding token1, need to sell
                            print(f"  One-sided position (token1={pos_1:.2f}), selling at market")
                            deets = get_best_bid_ask_deets(market, 'token1', 100, 0.1)
                            if deets and deets.get('best_bid'):
                                order = {
                                    'token': int(row['token1']),
                                    'price': deets['best_bid'],
                                    'size': pos_1,
                                    'neg_risk': row['neg_risk'],
                                    'mid_price': (deets['best_bid'] + deets['best_ask']) / 2,
                                    'max_spread': row.get('max_spread', 10),
                                    'orders': {'buy': {'price': 0, 'size': 0}, 'sell': {'price': 0, 'size': 0}},
                                    'row': row
                                }
                                try:
                                    send_sell_order(order)
                                    exit_success = True
                                except Exception as e:
                                    print(f"  Error sending sell order: {e}")

                        elif pos_2 > 0 and pos_1 == 0:
                            # One-sided: only holding token2, need to sell
                            print(f"  One-sided position (token2={pos_2:.2f}), selling at market")
                            deets = get_best_bid_ask_deets(market, 'token2', 100, 0.1)
                            if deets and deets.get('best_bid'):
                                order = {
                                    'token': int(row['token2']),
                                    'price': deets['best_bid'],
                                    'size': pos_2,
                                    'neg_risk': row['neg_risk'],
                                    'mid_price': (deets['best_bid'] + deets['best_ask']) / 2,
                                    'max_spread': row.get('max_spread', 10),
                                    'orders': {'buy': {'price': 0, 'size': 0}, 'sell': {'price': 0, 'size': 0}},
                                    'row': row
                                }
                                try:
                                    send_sell_order(order)
                                    exit_success = True
                                except Exception as e:
                                    print(f"  Error sending sell order: {e}")

                        elif pos_1 > 0 and pos_2 > 0:
                            # Balanced position - safe to hold through resolution
                            print(f"  Balanced position (token1={pos_1:.2f}, token2={pos_2:.2f}), safe to resolve")
                            exit_success = True  # No action needed, but mark as handled
                        else:
                            print(f"  No position to exit")
                            exit_success = True  # No action needed, but mark as handled

                        # Mark this market as attempted (success or failure)
                        # to prevent repeated attempts
                        _exit_attempted_markets.add(market)
                        if not exit_success:
                            print(f"  Exit attempt failed - will not retry automatically")

                        # Skip all other trading logic for this market
                        gc.collect()
                        await asyncio.sleep(2)
                        return

            # ------- POSITION MERGING LOGIC -------
            # Calculate if we have opposing positions that can be merged
            amount_to_merge = min(pos_1, pos_2)
            
            # Only merge if positions are above minimum threshold
            if float(amount_to_merge) > CONSTANTS.MIN_MERGE_SIZE:
                # Get exact position sizes from blockchain for merging
                pos_1 = client.get_position(row['token1'])[0]
                pos_2 = client.get_position(row['token2'])[0]
                amount_to_merge = min(pos_1, pos_2)
                scaled_amt = amount_to_merge / 10**6
                
                if scaled_amt > CONSTANTS.MIN_MERGE_SIZE:
                    print(f"Position 1 is of size {pos_1} and Position 2 is of size {pos_2}. Merging positions")
                    # Execute the merge operation
                    client.merge_positions(amount_to_merge, market, row['neg_risk'] == 'TRUE')
                    # Update our local position tracking
                    set_position(row['token1'], 'SELL', scaled_amt, 0, 'merge')
                    set_position(row['token2'], 'SELL', scaled_amt, 0, 'merge')
                    
            # ------- TRADING LOGIC FOR EACH OUTCOME -------
            # Loop through both outcomes in the market (YES and NO)
            for detail in deets:
                token = int(detail['token'])
                
                # Get current orders for this token
                orders = get_order(token)

                # Get market depth and price information
                deets = get_best_bid_ask_deets(market, detail['name'], 100, 0.1)

                #if deet has None for one these values below, call it with min size of 20
                if deets['best_bid'] is None or deets['best_ask'] is None or deets['best_bid_size'] is None or deets['best_ask_size'] is None:
                    deets = get_best_bid_ask_deets(market, detail['name'], 20, 0.1)
                
                # Extract all order book details
                best_bid = deets['best_bid']
                best_bid_size = deets['best_bid_size']
                second_best_bid = deets['second_best_bid']
                second_best_bid_size = deets['second_best_bid_size'] 
                top_bid = deets['top_bid']
                best_ask = deets['best_ask']
                best_ask_size = deets['best_ask_size']
                second_best_ask = deets['second_best_ask']
                second_best_ask_size = deets['second_best_ask_size']
                top_ask = deets['top_ask']
                
                # Round prices to appropriate precision
                best_bid = round(best_bid, round_length)
                best_ask = round(best_ask, round_length)

                # Calculate ratio of buy vs sell liquidity in the market
                try:
                    overall_ratio = (deets['bid_sum_within_n_percent']) / (deets['ask_sum_within_n_percent'])
                except:
                    overall_ratio = 0

                try:
                    second_best_bid = round(second_best_bid, round_length)
                    second_best_ask = round(second_best_ask, round_length)
                except:
                    pass
                
                top_bid = round(top_bid, round_length)
                top_ask = round(top_ask, round_length)

                # Get our current position and average price
                pos = get_position(token)
                position = pos['size']
                avgPrice = pos['avgPrice']
                
                position = round_down(position, 2)
               
                # Calculate optimal bid and ask prices based on market conditions
                bid_price, ask_price = get_order_prices(
                    best_bid, best_bid_size, top_bid, best_ask, 
                    best_ask_size, top_ask, avgPrice, row
                )

                bid_price = round(bid_price, round_length)
                ask_price = round(ask_price, round_length)

                # Calculate mid price for reference
                mid_price = (top_bid + top_ask) / 2
                
                # Log market conditions for this outcome
                print(f"\nFor {detail['answer']}. Orders: {orders} Position: {position}, "
                      f"avgPrice: {avgPrice}, Best Bid: {best_bid}, Best Ask: {best_ask}, "
                      f"Bid Price: {bid_price}, Ask Price: {ask_price}, Mid Price: {mid_price}")

                # Get position for the opposite token to calculate total exposure
                other_token = global_state.REVERSE_TOKENS[str(token)]
                other_position = get_position(other_token)['size']
                
                # Calculate how much to buy or sell based on our position
                buy_amount, sell_amount = get_buy_sell_amount(position, bid_price, row, other_position)
                
                # Get max_size for logging (same logic as in get_buy_sell_amount)
                max_size = row.get('max_size', row['trade_size'])

                # Prepare order object with all necessary information
                order = {
                    "token": token,
                    "mid_price": mid_price,
                    "neg_risk": row['neg_risk'],
                    "max_spread": row['max_spread'],
                    'orders': orders,
                    'token_name': detail['name'],
                    'row': row
                }
            
                print(f"Position: {position}, Other Position: {other_position}, "
                      f"Trade Size: {row['trade_size']}, Max Size: {max_size}, "
                      f"buy_amount: {buy_amount}, sell_amount: {sell_amount}")

                # File to store risk management information for this market
                fname = 'positions/' + str(market) + '.json'

                # ------- SELL ORDER LOGIC -------
                if sell_amount > 0:
                    # Skip if we have no average price (no real position)
                    if avgPrice == 0:
                        print("Avg Price is 0. Skipping")
                        continue

                    order['size'] = sell_amount
                    order['price'] = ask_price

                    # Get fresh market data for risk assessment
                    n_deets = get_best_bid_ask_deets(market, detail['name'], 100, 0.1)
                    
                    # Calculate current market price and spread
                    mid_price = round_up((n_deets['best_bid'] + n_deets['best_ask']) / 2, round_length)
                    spread = round(n_deets['best_ask'] - n_deets['best_bid'], 2)

                    # Calculate current profit/loss on position
                    pnl = (mid_price - avgPrice) / avgPrice * 100

                    print(f"Mid Price: {mid_price}, Spread: {spread}, PnL: {pnl}")
                    
                    # Prepare risk details for tracking
                    risk_details = {
                        'time': str(pd.Timestamp.utcnow().tz_localize(None)),
                        'question': row['question']
                    }

                    try:
                        ratio = (n_deets['bid_sum_within_n_percent']) / (n_deets['ask_sum_within_n_percent'])
                    except:
                        ratio = 0

                    pos_to_sell = sell_amount  # Amount to sell in risk-off scenario

                    # ------- STOP-LOSS LOGIC -------
                    # Trigger stop-loss if either:
                    # 1. PnL is below threshold and spread is tight enough to exit
                    # 2. Volatility is too high
                    if (pnl < params['stop_loss_threshold'] and spread <= params['spread_threshold']) or row['3_hour'] > params['volatility_threshold']:
                        risk_details['msg'] = (f"Selling {pos_to_sell} because spread is {spread} and pnl is {pnl} "
                                              f"and ratio is {ratio} and 3 hour volatility is {row['3_hour']}")
                        print("Stop loss Triggered: ", risk_details['msg'])

                        # Sell at market best bid to ensure execution
                        order['size'] = pos_to_sell
                        order['price'] = n_deets['best_bid']

                        # Set period to avoid trading after stop-loss
                        risk_details['sleep_till'] = str(pd.Timestamp.utcnow().tz_localize(None) + 
                                                        pd.Timedelta(hours=params['sleep_period']))

                        print("Risking off")
                        send_sell_order(order)
                        client.cancel_all_market(market)

                        # Save risk details to file
                        open(fname, 'w').write(json.dumps(risk_details))
                        continue

                # ------- BUY ORDER LOGIC -------
                # Get max_size, defaulting to trade_size if not specified
                max_size = row.get('max_size', row['trade_size'])
                
                # Only buy if:
                # 1. Position is less than max_size (new logic)
                # 2. Position is less than absolute cap (250)
                # 3. Buy amount is above minimum size
                if position < max_size and position < 250 and buy_amount > 0 and buy_amount >= row['min_size']:
                    # Get reference price from market data
                    sheet_value = row['best_bid']

                    if detail['name'] == 'token2':
                        sheet_value = 1 - row['best_ask']

                    sheet_value = round(sheet_value, round_length)
                    order['size'] = buy_amount
                    order['price'] = bid_price

                    # Check if price is far from reference
                    price_change = abs(order['price'] - sheet_value)

                    send_buy = True

                    # ------- RISK-OFF PERIOD CHECK -------
                    # If we're in a risk-off period (after stop-loss), don't buy
                    if os.path.isfile(fname):
                        risk_details = json.load(open(fname))

                        start_trading_at = pd.to_datetime(risk_details['sleep_till'])
                        current_time = pd.Timestamp.utcnow().tz_localize(None)

                        print(risk_details, current_time, start_trading_at)
                        if current_time < start_trading_at:
                            send_buy = False
                            print(f"Not sending a buy order because recently risked off. "
                                 f"Risked off at {risk_details['time']}")

                    # Only proceed if we're not in risk-off period
                    if send_buy:
                        # Don't buy if volatility is high or price is far from reference
                        if row['3_hour'] > params['volatility_threshold'] or price_change >= 0.05:
                            print(f'3 Hour Volatility of {row["3_hour"]} is greater than max volatility of '
                                  f'{params["volatility_threshold"]} or price of {order["price"]} is outside '
                                  f'0.05 of {sheet_value}. Skipping new orders.')
                            # Only cancel if we have orders - don't cancel repeatedly
                            if orders['buy']['size'] > 0:
                                print(f'Cancelling existing orders due to volatility/price conditions')
                                client.cancel_all_asset(order['token'])
                        else:
                            # Check for reverse position (holding opposite outcome)
                            rev_token = global_state.REVERSE_TOKENS[str(token)]
                            rev_pos = get_position(rev_token)

                            # If we have significant opposing position, don't buy more
                            if rev_pos['size'] > row['min_size']:
                                print("Bypassing creation of new buy order because there is a reverse position")
                                if orders['buy']['size'] > CONSTANTS.MIN_MERGE_SIZE:
                                    print("Cancelling buy orders because there is a reverse position")
                                    client.cancel_all_asset(order['token'])
                                
                                continue
                            
                            # Check market buy/sell volume ratio
                            if overall_ratio < 0:
                                send_buy = False
                                print(f"Not sending a buy order because overall ratio is {overall_ratio}")
                                # Only cancel if we have orders - don't cancel repeatedly
                                if orders['buy']['size'] > 0:
                                    client.cancel_all_asset(order['token'])
                            else:
                                # Place new buy order if any of these conditions are met:
                                # 1. We can get a better price than current order
                                if best_bid > orders['buy']['price']:
                                    print(f"Sending Buy Order for {token} because better price. "
                                          f"Orders look like this: {orders['buy']}. Best Bid: {best_bid}")
                                    send_buy_order(order)
                                # 2. Current position + orders is not enough to reach max_size
                                elif position + orders['buy']['size'] < 0.95 * max_size:
                                    print(f"Sending Buy Order for {token} because not enough position + size")
                                    send_buy_order(order)
                                # 3. Our current order is too large and needs to be resized
                                elif orders['buy']['size'] > order['size'] * 1.01:
                                    print(f"Resending buy orders because open orders are too large")
                                    send_buy_order(order)
                                # Commented out logic for cancelling orders when market conditions change
                                # elif best_bid_size < orders['buy']['size'] * 0.98 and abs(best_bid - second_best_bid) > 0.03:
                                #     print(f"Cancelling buy orders because best size is less than 90% of open orders and spread is too large")
                                #     global_state.client.cancel_all_asset(order['token'])
                        
                # ------- TAKE PROFIT / SELL ORDER MANAGEMENT -------            
                elif sell_amount > 0:
                    order['size'] = sell_amount
                    
                    # Calculate take-profit price based on average cost
                    tp_price = round_up(avgPrice + (avgPrice * params['take_profit_threshold']/100), round_length)
                    order['price'] = round_up(tp_price if ask_price < tp_price else ask_price, round_length)
                    
                    tp_price = float(tp_price)
                    order_price = float(orders['sell']['price'])
                    
                    # Calculate % difference between current order and ideal price
                    diff = abs(order_price - tp_price)/tp_price * 100

                    # Update sell order if:
                    # 1. Current order price is significantly different from target
                    if diff > 2:
                        print(f"Sending Sell Order for {token} because better current order price of "
                              f"{order_price} is deviant from the tp_price of {tp_price} and diff is {diff}")
                        send_sell_order(order)
                    # 2. Current order size is too small for our position
                    elif orders['sell']['size'] < position * 0.97:
                        print(f"Sending Sell Order for {token} because not enough sell size. "
                              f"Position: {position}, Sell Size: {orders['sell']['size']}")
                        send_sell_order(order)
                    
                    # Commented out additional conditions for updating sell orders
                    # elif orders['sell']['price'] < ask_price:
                    #     print(f"Updating Sell Order for {token} because its not at the right price")
                    #     send_sell_order(order)
                    # elif best_ask_size < orders['sell']['size'] * 0.98 and abs(best_ask - second_best_ask) > 0.03...:
                    #     print(f"Cancelling sell orders because best size is less than 90% of open orders...")
                    #     send_sell_order(order)

        except Exception as ex:
            print(f"Error performing trade for {market}: {ex}")
            traceback.print_exc()

        # Clean up memory and introduce a small delay
        gc.collect()
        await asyncio.sleep(2)