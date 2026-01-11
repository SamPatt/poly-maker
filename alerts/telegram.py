"""
Telegram alerts module for Poly-Maker.
Sends notifications for trades, errors, and daily summaries.
"""

import os
import asyncio
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Check if Telegram is configured
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

if not TELEGRAM_ENABLED:
    print("Telegram alerts disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")


async def _send_telegram_message(message: str) -> bool:
    """
    Send a message via Telegram bot API.

    Args:
        message: Message text to send

    Returns:
        True if sent successfully
    """
    if not TELEGRAM_ENABLED:
        return False

    try:
        from telegram import Bot

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")
        return False


def send_alert(message: str) -> bool:
    """
    Send a generic alert message.

    Args:
        message: Message to send

    Returns:
        True if sent successfully
    """
    if not TELEGRAM_ENABLED:
        print(f"[ALERT - Telegram disabled] {message}")
        return False

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_send_telegram_message(message))
            return True
        else:
            return asyncio.run(_send_telegram_message(message))
    except RuntimeError:
        return asyncio.run(_send_telegram_message(message))


def send_trade_alert(
    side: str,
    token: str,
    price: float,
    size: float,
    market_question: Optional[str] = None,
    outcome: Optional[str] = None
) -> bool:
    """
    Send an alert when a trade is executed.

    Args:
        side: 'BUY' or 'SELL'
        token: Token ID
        price: Execution price
        size: Trade size
        market_question: Optional market question for context
        outcome: Optional outcome name (e.g., 'Yes', 'No', 'Up', 'Down')

    Returns:
        True if sent successfully
    """
    emoji = "🟢" if side.upper() == "BUY" else "🔴"
    side_text = side.upper()

    message = f"{emoji} <b>Trade Executed</b>\n\n"

    # Show market first for context
    if market_question:
        # Truncate long questions
        if len(market_question) > 80:
            market_question = market_question[:77] + "..."
        message += f"<b>Market:</b> {market_question}\n\n"

    message += f"<b>Side:</b> {side_text}"
    if outcome:
        message += f" <b>{outcome.upper()}</b>"
    message += f"\n"
    message += f"<b>Price:</b> ${price:.4f}\n"
    message += f"<b>Size:</b> ${size:.2f}"

    return send_alert(message)


def send_error_alert(error_type: str, error_message: str, context: Optional[str] = None) -> bool:
    """
    Send an alert when an error occurs.

    Args:
        error_type: Type of error (e.g., 'WebSocket', 'Order', 'API')
        error_message: Error message details
        context: Optional additional context

    Returns:
        True if sent successfully
    """
    message = f"⚠️ <b>Error: {error_type}</b>\n\n"
    message += f"<code>{error_message[:500]}</code>"  # Limit message length

    if context:
        message += f"\n\n<b>Context:</b> {context}"

    return send_alert(message)


def send_stop_loss_alert(
    market_question: str,
    pnl: float,
    position_size: float,
    exit_price: float
) -> bool:
    """
    Send an alert when stop-loss is triggered.

    Args:
        market_question: Market question
        pnl: Profit/loss percentage
        position_size: Size of position being closed
        exit_price: Price at which position is being closed

    Returns:
        True if sent successfully
    """
    message = f"🛑 <b>Stop-Loss Triggered</b>\n\n"
    message += f"<b>Market:</b> {market_question[:100]}\n"
    message += f"<b>P&L:</b> {pnl:.2f}%\n"
    message += f"<b>Position:</b> ${position_size:.2f}\n"
    message += f"<b>Exit Price:</b> ${exit_price:.4f}\n"
    message += f"\n<i>Trading paused for this market</i>"

    return send_alert(message)


def send_position_merged_alert(
    market_question: str,
    amount_merged: float,
    usdc_recovered: float
) -> bool:
    """
    Send an alert when positions are merged.

    Args:
        market_question: Market question
        amount_merged: Amount of tokens merged
        usdc_recovered: USDC recovered from merge

    Returns:
        True if sent successfully
    """
    message = f"🔄 <b>Positions Merged</b>\n\n"
    message += f"<b>Market:</b> {market_question[:100]}\n"
    message += f"<b>Amount:</b> {amount_merged:.2f} tokens\n"
    message += f"<b>USDC Recovered:</b> ${usdc_recovered:.2f}"

    return send_alert(message)


def send_daily_summary(
    total_positions: int,
    total_value: float,
    daily_pnl: float,
    daily_trades: int,
    earnings: float
) -> bool:
    """
    Send daily summary alert (typically scheduled for 6am UTC).

    Args:
        total_positions: Number of active positions
        total_value: Total portfolio value
        daily_pnl: Day's profit/loss
        daily_trades: Number of trades executed today
        earnings: Total earnings from rewards

    Returns:
        True if sent successfully
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl_emoji = "📈" if daily_pnl >= 0 else "📉"

    message = f"📊 <b>Daily Summary - {timestamp}</b>\n\n"
    message += f"<b>Portfolio Value:</b> ${total_value:.2f}\n"
    message += f"<b>Active Positions:</b> {total_positions}\n"
    message += f"{pnl_emoji} <b>Today's P&L:</b> ${daily_pnl:.2f}\n"
    message += f"<b>Trades Today:</b> {daily_trades}\n"
    message += f"<b>Earnings:</b> ${earnings:.2f}"

    return send_alert(message)


def send_startup_alert(dry_run: bool = False) -> bool:
    """
    Send alert when bot starts up.

    Args:
        dry_run: Whether bot is running in dry-run mode

    Returns:
        True if sent successfully
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    emoji = "🧪" if dry_run else "🚀"

    message = f"{emoji} <b>Poly-Maker Started</b>\n\n"
    message += f"<b>Mode:</b> {mode}\n"
    message += f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

    return send_alert(message)


def send_shutdown_alert(reason: str = "Normal shutdown") -> bool:
    """
    Send alert when bot shuts down.

    Args:
        reason: Reason for shutdown

    Returns:
        True if sent successfully
    """
    message = f"🔴 <b>Poly-Maker Stopped</b>\n\n"
    message += f"<b>Reason:</b> {reason}\n"
    message += f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

    return send_alert(message)


# ============================================
# Rebates Bot Alerts
# ============================================

def send_rebates_startup_alert(dry_run: bool, trade_size: float) -> bool:
    """
    Send alert when rebates bot starts.

    Args:
        dry_run: Whether bot is in dry-run mode
        trade_size: Trade size per side

    Returns:
        True if sent successfully
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    emoji = "🧪" if dry_run else "🚀"

    message = f"{emoji} <b>Rebates Bot Started</b>\n\n"
    message += f"<b>Mode:</b> {mode}\n"
    message += f"<b>Trade Size:</b> ${trade_size:.2f} per side\n"
    message += f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

    return send_alert(message)


def send_rebates_order_alert(
    question: str,
    trade_size: float,
    price: float,
    dry_run: bool = False
) -> bool:
    """
    Send alert when rebates orders are placed.

    Args:
        question: Market question
        trade_size: Size per side
        price: Order price
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    emoji = "🧪" if dry_run else "💰"
    mode_text = "[DRY RUN] " if dry_run else ""

    # Truncate long questions
    if len(question) > 80:
        question = question[:77] + "..."

    message = f"{emoji} <b>{mode_text}Rebates Orders Placed</b>\n\n"
    message += f"<b>Market:</b> {question}\n"
    message += f"<b>Price:</b> {price:.2f}\n"
    message += f"<b>Size:</b> ${trade_size:.2f} x 2 (UP + DOWN)\n"
    message += f"<b>Total:</b> ${trade_size * 2:.2f}"

    return send_alert(message)


def send_rebates_resolution_alert(
    question: str,
    up_filled: bool,
    down_filled: bool,
    trade_size: float,
    dry_run: bool = False
) -> bool:
    """
    Send alert when a rebates market resolves.

    Args:
        question: Market question
        up_filled: Whether UP order was filled
        down_filled: Whether DOWN order was filled
        trade_size: Trade size per side
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    if dry_run:
        message = f"🧪 <b>[DRY RUN] Market Resolved</b>\n\n"
        message += f"<b>Market:</b> {question[:80]}\n"
        message += f"<b>Status:</b> Simulated"
        return send_alert(message)

    # Determine result
    if up_filled and down_filled:
        emoji = "✅"
        status = "Both Orders Filled"
        rebate_volume = trade_size * 2
    elif up_filled or down_filled:
        emoji = "⚠️"
        status = "One Order Filled"
        rebate_volume = trade_size
    else:
        emoji = "❌"
        status = "No Fills - Orders Expired"
        rebate_volume = 0

    # Truncate long questions
    if len(question) > 80:
        question = question[:77] + "..."

    message = f"{emoji} <b>Market Resolved</b>\n\n"
    message += f"<b>Market:</b> {question}\n"
    message += f"<b>UP:</b> {'FILLED' if up_filled else 'UNFILLED'}\n"
    message += f"<b>DOWN:</b> {'FILLED' if down_filled else 'UNFILLED'}\n"
    message += f"<b>Status:</b> {status}\n"
    if rebate_volume > 0:
        message += f"<b>Rebate Volume:</b> ${rebate_volume:.2f}"

    return send_alert(message)


def send_rebates_rescue_alert(
    question: str,
    side: str,
    old_price: float,
    new_price: float,
    is_taker: bool = False,
    dry_run: bool = False
) -> bool:
    """
    Send alert when a rescue order is placed/filled.

    Args:
        question: Market question
        side: "UP" or "DOWN"
        old_price: Original order price
        new_price: New rescue price
        is_taker: Whether this was a taker (market) order
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    if dry_run:
        return False

    # Truncate long questions
    if len(question) > 60:
        question = question[:57] + "..."

    if is_taker:
        emoji = "🚨"
        message = f"{emoji} <b>Rescue Taker Order</b>\n\n"
        message += f"<b>Market:</b> {question}\n"
        message += f"<b>Side:</b> {side}\n"
        message += f"<b>Price:</b> ${new_price:.2f} (crossed spread)\n"
        message += f"<b>Reason:</b> <30s to resolution"
    else:
        emoji = "🔄"
        message = f"{emoji} <b>Rescue Order Updated</b>\n\n"
        message += f"<b>Market:</b> {question}\n"
        message += f"<b>Side:</b> {side}\n"
        message += f"<b>Price:</b> ${old_price:.2f} → ${new_price:.2f}"

    return send_alert(message)


def send_rebates_fill_alert(
    question: str,
    side: str,
    price: float,
    size: float,
    dry_run: bool = False
) -> bool:
    """
    Send alert when a rebates order is filled.

    Args:
        question: Market question
        side: "UP" or "DOWN"
        price: Fill price
        size: Fill size in $
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    if dry_run:
        return False

    # Truncate long questions
    if len(question) > 60:
        question = question[:57] + "..."

    emoji = "🎯"
    message = f"{emoji} <b>Rebates Order Filled</b>\n\n"
    message += f"<b>Market:</b> {question}\n"
    message += f"<b>Side:</b> {side}\n"
    message += f"<b>Price:</b> ${price:.2f}\n"
    message += f"<b>Size:</b> ${size:.2f}"

    return send_alert(message)


def send_rebates_rescue_filled_alert(
    question: str,
    side: str,
    dry_run: bool = False
) -> bool:
    """
    Send alert when a rescue order is filled.

    Args:
        question: Market question
        side: "UP" or "DOWN"
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    if dry_run:
        return False

    # Truncate long questions
    if len(question) > 60:
        question = question[:57] + "..."

    emoji = "✅"
    message = f"{emoji} <b>Rescue Order Filled</b>\n\n"
    message += f"<b>Market:</b> {question}\n"
    message += f"<b>Side:</b> {side}"

    return send_alert(message)


def send_rebates_redemption_alert(
    question: str,
    condition_id: str,
    dry_run: bool = False
) -> bool:
    """
    Send alert when positions are successfully redeemed.

    Args:
        question: Market question
        condition_id: Market condition ID
        dry_run: Whether in dry-run mode

    Returns:
        True if sent successfully
    """
    if dry_run:
        message = f"🧪 <b>[DRY RUN] Would Redeem</b>\n\n"
        message += f"<b>Market:</b> {question[:80]}\n"
        message += f"<b>Condition:</b> {condition_id[:20]}..."
        return send_alert(message)

    # Truncate long questions
    if len(question) > 80:
        question = question[:77] + "..."

    message = f"💵 <b>Positions Redeemed</b>\n\n"
    message += f"<b>Market:</b> {question}\n"
    message += f"<b>Condition:</b> <code>{condition_id[:20]}...</code>\n"
    message += f"<b>Status:</b> USDC recovered"

    return send_alert(message)


# ============================================
# Enhanced Error and Monitoring Alerts
# ============================================


def send_critical_error_alert(
    error_type: str,
    message: str,
    context: Optional[dict] = None
) -> bool:
    """
    Send alert for critical errors that require immediate attention.

    Use this for errors that have should_alert=True in the exception hierarchy.
    Includes error type classification and context.

    Args:
        error_type: Type classification (e.g., 'insufficient_balance', 'order_creation')
        message: Error message
        context: Optional dict with additional context

    Returns:
        True if sent successfully
    """
    emoji_map = {
        'insufficient_balance': '💸',
        'order_creation': '❌',
        'stop_loss': '🛑',
        'position_merge': '🔄',
        'websocket': '🔌',
        'blockchain': '⛓️',
        'polymarket_api': '🌐',
        'redemption': '💵',
        'database': '🗄️',
        'state_inconsistency': '⚠️',
        'configuration': '⚙️',
    }

    emoji = emoji_map.get(error_type, '⚠️')

    alert_msg = f"{emoji} <b>Error: {error_type.replace('_', ' ').title()}</b>\n\n"
    alert_msg += f"<code>{message[:400]}</code>"

    if context:
        alert_msg += "\n\n<b>Context:</b>\n"
        for key, value in list(context.items())[:5]:
            # Truncate long values
            str_value = str(value)
            if len(str_value) > 50:
                str_value = str_value[:47] + "..."
            alert_msg += f"• {key}: {str_value}\n"

    return send_alert(alert_msg)


def send_websocket_reconnect_alert(
    connection_type: str,
    attempt: int,
    error: str
) -> bool:
    """
    Alert when WebSocket requires reconnection.

    Only sends alert after multiple failures to avoid noise.

    Args:
        connection_type: 'market' or 'user'
        attempt: Current reconnection attempt number
        error: Error message that caused reconnection

    Returns:
        True if sent successfully (or False if attempt < 3)
    """
    # Only alert after 3+ failures to reduce noise
    if attempt < 3:
        return False

    message = f"🔌 <b>WebSocket Reconnecting</b>\n\n"
    message += f"<b>Type:</b> {connection_type}\n"
    message += f"<b>Attempts:</b> {attempt}\n"
    message += f"<b>Error:</b> {error[:100]}"

    return send_alert(message)


def send_balance_warning_alert(
    current_balance: float,
    committed: float,
    threshold: float
) -> bool:
    """
    Alert when available balance is running low.

    Args:
        current_balance: Current USDC wallet balance
        committed: Funds committed to open orders
        threshold: Minimum balance threshold

    Returns:
        True if sent successfully
    """
    available = current_balance - committed

    message = f"💰 <b>Low Balance Warning</b>\n\n"
    message += f"<b>Wallet:</b> ${current_balance:.2f}\n"
    message += f"<b>Committed:</b> ${committed:.2f}\n"
    message += f"<b>Available:</b> ${available:.2f}\n"
    message += f"<b>Threshold:</b> ${threshold:.2f}\n"
    message += f"\n<i>New buy orders may be blocked</i>"

    return send_alert(message)


def send_market_exit_alert(
    market_question: str,
    reason: str,
    position_value: float,
    exit_price: Optional[float] = None
) -> bool:
    """
    Alert when exiting a market (exit_before_event or manual exit).

    Args:
        market_question: Market question text
        reason: Reason for exit (e.g., 'Exit before event', 'Manual exit')
        position_value: Value of position being exited
        exit_price: Price at which position was sold (optional)

    Returns:
        True if sent successfully
    """
    # Truncate long questions
    if len(market_question) > 80:
        market_question = market_question[:77] + "..."

    message = f"🚪 <b>Market Exit</b>\n\n"
    message += f"<b>Market:</b> {market_question}\n"
    message += f"<b>Reason:</b> {reason}\n"
    message += f"<b>Position Value:</b> ${position_value:.2f}"

    if exit_price is not None:
        message += f"\n<b>Exit Price:</b> ${exit_price:.4f}"

    return send_alert(message)


def send_high_volatility_alert(
    market_question: str,
    volatility_3h: float,
    threshold: float
) -> bool:
    """
    Alert when market volatility exceeds threshold.

    Args:
        market_question: Market question text
        volatility_3h: 3-hour volatility percentage
        threshold: Volatility threshold that was exceeded

    Returns:
        True if sent successfully
    """
    # Truncate long questions
    if len(market_question) > 80:
        market_question = market_question[:77] + "..."

    message = f"📊 <b>High Volatility Alert</b>\n\n"
    message += f"<b>Market:</b> {market_question}\n"
    message += f"<b>3h Volatility:</b> {volatility_3h:.2f}%\n"
    message += f"<b>Threshold:</b> {threshold:.2f}%\n"
    message += f"\n<i>Trading may be paused for this market</i>"

    return send_alert(message)


def send_order_fill_alert(
    side: str,
    token: str,
    price: float,
    size: float,
    market_question: Optional[str] = None,
    pnl: Optional[float] = None
) -> bool:
    """
    Alert when an order is filled (trade executed).

    Enhanced version of send_trade_alert with P&L information.

    Args:
        side: 'BUY' or 'SELL'
        token: Token ID
        price: Fill price
        size: Trade size
        market_question: Optional market question
        pnl: Optional profit/loss on this trade

    Returns:
        True if sent successfully
    """
    emoji = "🟢" if side.upper() == "BUY" else "🔴"

    message = f"{emoji} <b>Order Filled</b>\n\n"

    if market_question:
        if len(market_question) > 80:
            market_question = market_question[:77] + "..."
        message += f"<b>Market:</b> {market_question}\n\n"

    message += f"<b>Side:</b> {side.upper()}\n"
    message += f"<b>Price:</b> ${price:.4f}\n"
    message += f"<b>Size:</b> ${size:.2f}"

    if pnl is not None:
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        message += f"\n{pnl_emoji} <b>P&L:</b> ${pnl:.2f}"

    return send_alert(message)
