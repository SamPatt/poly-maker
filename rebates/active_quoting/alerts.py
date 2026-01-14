"""
Active Quoting Alerts - Telegram alert functions for ActiveQuotingBot.

Provides specific alert functions for:
- Fill notifications (with throttling)
- Startup/shutdown alerts
- Circuit breaker state transitions
- Daily summary

Integrates with existing alerts/telegram.py infrastructure.
"""
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict

from alerts.telegram import send_alert, TELEGRAM_ENABLED


# Throttle configuration
FILL_ALERT_THROTTLE_SECONDS = 10  # Max 1 fill alert per market per 10 seconds
FILL_BATCH_SIZE = 5  # Batch fills into groups


@dataclass
class FillAlertThrottler:
    """
    Throttles fill alerts to prevent spam during rapid fills.

    Batches fills and sends periodic summaries instead of individual alerts.
    """

    # Per-market last alert time
    last_alert_time: Dict[str, float] = field(default_factory=dict)
    # Per-market pending fills
    pending_fills: Dict[str, list] = field(default_factory=lambda: defaultdict(list))
    # Global fill count since last summary
    fill_count_since_summary: int = 0

    def should_send_fill_alert(self, market_name: str) -> bool:
        """Check if we should send an alert for this market."""
        now = time.time()
        last = self.last_alert_time.get(market_name, 0)
        return (now - last) >= FILL_ALERT_THROTTLE_SECONDS

    def record_fill(
        self,
        market_name: str,
        side: str,
        price: float,
        size: float,
        markout_bps: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Record a fill and return batch if ready to send.

        Returns:
            Dict with batch info if ready to send, None otherwise
        """
        self.pending_fills[market_name].append({
            "side": side,
            "price": price,
            "size": size,
            "markout_bps": markout_bps,
            "timestamp": time.time(),
        })
        self.fill_count_since_summary += 1

        if self.should_send_fill_alert(market_name):
            fills = self.pending_fills[market_name]
            self.pending_fills[market_name] = []
            self.last_alert_time[market_name] = time.time()
            return {
                "market_name": market_name,
                "fills": fills,
            }
        return None

    def flush_all(self) -> list:
        """Flush all pending fills and return batches."""
        batches = []
        for market_name, fills in self.pending_fills.items():
            if fills:
                batches.append({
                    "market_name": market_name,
                    "fills": fills,
                })
        self.pending_fills = defaultdict(list)
        return batches


# Global throttler instance
_fill_throttler = FillAlertThrottler()


def send_active_quoting_startup_alert(
    market_count: int,
    dry_run: bool = False,
    config_summary: Optional[dict] = None,
) -> bool:
    """
    Send alert when ActiveQuotingBot starts.

    Args:
        market_count: Number of markets being quoted
        dry_run: Whether bot is in dry-run mode
        config_summary: Optional config summary (order_size, etc.)

    Returns:
        True if sent successfully
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    emoji = "🧪" if dry_run else "🚀"

    message = f"{emoji} <b>Active Quoting Bot Started</b>\n\n"
    message += f"<b>Mode:</b> {mode}\n"
    message += f"<b>Markets:</b> {market_count}\n"

    if config_summary:
        if "order_size" in config_summary:
            message += f"<b>Order Size:</b> ${config_summary['order_size']:.2f}\n"
        if "max_position" in config_summary:
            message += f"<b>Max Position:</b> {config_summary['max_position']} shares\n"

    message += f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

    return send_alert(message, wait=True)


def send_active_quoting_shutdown_alert(
    reason: str = "Normal shutdown",
    stats: Optional[dict] = None,
) -> bool:
    """
    Send alert when ActiveQuotingBot stops.

    Args:
        reason: Reason for shutdown
        stats: Optional session statistics

    Returns:
        True if sent successfully
    """
    message = f"🔴 <b>Active Quoting Bot Stopped</b>\n\n"
    message += f"<b>Reason:</b> {reason}\n"

    if stats:
        if "total_fills" in stats:
            message += f"<b>Total Fills:</b> {stats['total_fills']}\n"
        if "net_fees" in stats:
            fee_emoji = "📈" if stats['net_fees'] >= 0 else "📉"
            message += f"{fee_emoji} <b>Net Fees:</b> ${stats['net_fees']:.2f}\n"
        if "realized_pnl" in stats:
            pnl_emoji = "📈" if stats['realized_pnl'] >= 0 else "📉"
            message += f"{pnl_emoji} <b>Realized P&L:</b> ${stats['realized_pnl']:.2f}\n"

    message += f"<b>Time:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"

    # Flush any pending fill alerts
    batches = _fill_throttler.flush_all()
    for batch in batches:
        _send_fill_batch_alert(batch)

    return send_alert(message, wait=True)


def send_active_quoting_fill_alert(
    market_name: str,
    side: str,
    price: float,
    size: float,
    markout_bps: Optional[float] = None,
    force: bool = False,
) -> bool:
    """
    Send alert on fill with throttling.

    Uses batching/throttling to prevent spam during rapid fills.

    Args:
        market_name: Market name/identifier
        side: "BUY" or "SELL"
        price: Fill price
        size: Fill size in shares
        markout_bps: Optional markout in basis points (if available)
        force: If True, bypass throttling

    Returns:
        True if alert was sent (or queued)
    """
    if force:
        return _send_single_fill_alert(market_name, side, price, size, markout_bps)

    batch = _fill_throttler.record_fill(
        market_name=market_name,
        side=side,
        price=price,
        size=size,
        markout_bps=markout_bps,
    )

    if batch:
        return _send_fill_batch_alert(batch)

    return True  # Queued for later


def _send_single_fill_alert(
    market_name: str,
    side: str,
    price: float,
    size: float,
    markout_bps: Optional[float] = None,
) -> bool:
    """Send a single fill alert."""
    emoji = "🟢" if side.upper() == "BUY" else "🔴"

    # Truncate long market names
    if len(market_name) > 40:
        market_name = market_name[:37] + "..."

    message = f"{emoji} <b>Active Quote Fill</b>\n\n"
    message += f"<b>Market:</b> {market_name}\n"
    message += f"<b>Side:</b> {side.upper()}\n"
    message += f"<b>Price:</b> ${price:.4f}\n"
    message += f"<b>Size:</b> {size:.2f} shares"

    if markout_bps is not None:
        mkout_emoji = "✅" if markout_bps >= 0 else "⚠️"
        message += f"\n{mkout_emoji} <b>Markout:</b> {markout_bps:.1f} bps"

    return send_alert(message)


def _send_fill_batch_alert(batch: dict) -> bool:
    """Send a batched fill alert."""
    market_name = batch["market_name"]
    fills = batch["fills"]

    if len(fills) == 1:
        # Single fill - send as normal
        f = fills[0]
        return _send_single_fill_alert(
            market_name, f["side"], f["price"], f["size"], f.get("markout_bps")
        )

    # Multiple fills - send summary
    buy_count = sum(1 for f in fills if f["side"].upper() == "BUY")
    sell_count = len(fills) - buy_count
    total_buy_size = sum(f["size"] for f in fills if f["side"].upper() == "BUY")
    total_sell_size = sum(f["size"] for f in fills if f["side"].upper() == "SELL")

    # Truncate long market names
    if len(market_name) > 40:
        market_name = market_name[:37] + "..."

    message = f"📊 <b>Active Quote Fills</b>\n\n"
    message += f"<b>Market:</b> {market_name}\n"
    message += f"<b>Fills:</b> {len(fills)} ({buy_count} buys, {sell_count} sells)\n"

    if total_buy_size > 0:
        message += f"<b>Buy Volume:</b> {total_buy_size:.2f} shares\n"
    if total_sell_size > 0:
        message += f"<b>Sell Volume:</b> {total_sell_size:.2f} shares\n"

    # Calculate average markout if available
    markouts = [f.get("markout_bps") for f in fills if f.get("markout_bps") is not None]
    if markouts:
        avg_markout = sum(markouts) / len(markouts)
        mkout_emoji = "✅" if avg_markout >= 0 else "⚠️"
        message += f"{mkout_emoji} <b>Avg Markout:</b> {avg_markout:.1f} bps"

    return send_alert(message)


def send_active_quoting_circuit_breaker_alert(
    old_state: str,
    new_state: str,
    reason: str,
    details: Optional[dict] = None,
) -> bool:
    """
    Send alert on circuit breaker state transition.

    Args:
        old_state: Previous circuit breaker state
        new_state: New circuit breaker state
        reason: Reason for transition
        details: Optional details (drawdown, errors, etc.)

    Returns:
        True if sent successfully
    """
    # Choose emoji based on severity
    emoji_map = {
        "NORMAL": "✅",
        "WARNING": "⚠️",
        "HALTED": "🚨",
        "RECOVERING": "🔄",
    }
    emoji = emoji_map.get(new_state.upper(), "⚠️")

    message = f"{emoji} <b>Circuit Breaker: {new_state}</b>\n\n"
    message += f"<b>Transition:</b> {old_state} → {new_state}\n"
    message += f"<b>Reason:</b> {reason}\n"

    if details:
        if "drawdown" in details:
            message += f"<b>Drawdown:</b> ${details['drawdown']:.2f}\n"
        if "consecutive_errors" in details:
            message += f"<b>Errors:</b> {details['consecutive_errors']}\n"
        if "stale_markets" in details:
            message += f"<b>Stale Markets:</b> {details['stale_markets']}\n"
        if "halt_reason" in details and details["halt_reason"] != "NONE":
            message += f"<b>Halt Reason:</b> {details['halt_reason']}\n"

    message += f"\n<b>Time:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"

    # HALTED is critical - wait for delivery
    wait = new_state.upper() == "HALTED"
    return send_alert(message, wait=wait)


def send_active_quoting_daily_summary(
    session_duration_hours: float,
    total_fills: int,
    total_volume: float,
    total_notional: float,
    net_fees: float,
    realized_pnl: float,
    markout_stats: Optional[dict] = None,
    market_count: int = 0,
) -> bool:
    """
    Send daily summary alert for active quoting.

    Args:
        session_duration_hours: How long the bot has been running
        total_fills: Total number of fills
        total_volume: Total volume in shares
        total_notional: Total notional value in USDC
        net_fees: Net fees (rebates earned - fees paid)
        realized_pnl: Realized P&L
        markout_stats: Optional markout statistics by horizon
        market_count: Number of markets quoted

    Returns:
        True if sent successfully
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pnl_emoji = "📈" if realized_pnl >= 0 else "📉"
    fee_emoji = "💰" if net_fees >= 0 else "💸"

    message = f"📊 <b>Active Quoting Summary - {timestamp}</b>\n\n"
    message += f"<b>Session:</b> {session_duration_hours:.1f} hours\n"
    message += f"<b>Markets:</b> {market_count}\n"
    message += f"<b>Total Fills:</b> {total_fills}\n"
    message += f"<b>Volume:</b> {total_volume:.2f} shares\n"
    message += f"<b>Notional:</b> ${total_notional:.2f}\n"
    message += f"{fee_emoji} <b>Net Fees:</b> ${net_fees:.2f}\n"
    message += f"{pnl_emoji} <b>Realized P&L:</b> ${realized_pnl:.2f}\n"

    if markout_stats:
        message += "\n<b>Markouts:</b>\n"
        for horizon, avg_bps in markout_stats.items():
            if avg_bps is not None:
                mkout_emoji = "✅" if avg_bps >= 0 else "⚠️"
                message += f"  {mkout_emoji} {horizon}s: {avg_bps:.1f} bps\n"

    return send_alert(message)


def send_active_quoting_error_alert(
    error_type: str,
    message_text: str,
    market_name: Optional[str] = None,
) -> bool:
    """
    Send alert for errors during active quoting.

    Args:
        error_type: Type of error (e.g., "order_placement", "websocket", etc.)
        message_text: Error message
        market_name: Optional market name for context

    Returns:
        True if sent successfully
    """
    message = f"⚠️ <b>Active Quoting Error: {error_type}</b>\n\n"

    if market_name:
        if len(market_name) > 40:
            market_name = market_name[:37] + "..."
        message += f"<b>Market:</b> {market_name}\n"

    # Truncate long error messages
    if len(message_text) > 200:
        message_text = message_text[:197] + "..."

    message += f"<code>{message_text}</code>"

    return send_alert(message)


def send_active_quoting_market_halt_alert(
    market_name: str,
    reason: str,
) -> bool:
    """
    Send alert when a specific market is halted.

    Args:
        market_name: Market name/identifier
        reason: Reason for halt

    Returns:
        True if sent successfully
    """
    # Truncate long market names
    if len(market_name) > 40:
        market_name = market_name[:37] + "..."

    message = f"🛑 <b>Market Halted</b>\n\n"
    message += f"<b>Market:</b> {market_name}\n"
    message += f"<b>Reason:</b> {reason}\n"
    message += f"\n<i>Quoting paused for this market</i>"

    return send_alert(message)


def send_active_quoting_redemption_alert(
    market_name: str,
    position_size: float,
    tx_hash: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> bool:
    """
    Send alert when a position is redeemed after market resolution.

    Args:
        market_name: Market name/identifier
        position_size: Size of the position that was redeemed
        tx_hash: Transaction hash (if successful)
        success: Whether redemption was successful
        error_message: Error message (if failed)

    Returns:
        True if sent successfully
    """
    # Truncate long market names
    if len(market_name) > 40:
        market_name = market_name[:37] + "..."

    if success:
        emoji = "💰"
        message = f"{emoji} <b>Position Redeemed</b>\n\n"
        message += f"<b>Market:</b> {market_name}\n"
        message += f"<b>Size:</b> {position_size:.2f} shares\n"
        if tx_hash:
            # Truncate tx hash for display
            tx_display = tx_hash[:20] + "..." if len(tx_hash) > 20 else tx_hash
            message += f"<b>Tx:</b> <code>{tx_display}</code>"
    else:
        emoji = "⚠️"
        message = f"{emoji} <b>Redemption Failed</b>\n\n"
        message += f"<b>Market:</b> {market_name}\n"
        message += f"<b>Size:</b> {position_size:.2f} shares\n"
        if error_message:
            # Truncate long error messages
            if len(error_message) > 100:
                error_message = error_message[:97] + "..."
            message += f"<b>Error:</b> <code>{error_message}</code>"

    return send_alert(message)


# Re-export TELEGRAM_ENABLED for checking if alerts are configured
__all__ = [
    "TELEGRAM_ENABLED",
    "send_active_quoting_startup_alert",
    "send_active_quoting_shutdown_alert",
    "send_active_quoting_fill_alert",
    "send_active_quoting_circuit_breaker_alert",
    "send_active_quoting_daily_summary",
    "send_active_quoting_error_alert",
    "send_active_quoting_market_halt_alert",
    "send_active_quoting_redemption_alert",
    "FillAlertThrottler",
]
