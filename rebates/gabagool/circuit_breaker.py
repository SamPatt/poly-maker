"""
Circuit Breaker for Gabagool Strategy

A critical safety system that automatically halts trading when risk thresholds are exceeded.
This prevents catastrophic losses from bugs, market anomalies, or cascading errors.

Ported from the production Rust arbitrage bot in /sampatt/cledo/reference/.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerConfig:
    """
    Circuit breaker configuration.

    Ported from Rust reference implementation which uses these limits
    for production cross-platform arbitrage trading.
    """

    # Position limits
    max_position_per_market: float = 500.0  # Max $ exposure per market
    max_total_position: float = 2000.0  # Max $ total exposure across all markets

    # Loss limits
    max_daily_loss: float = 100.0  # Max $ loss before halt (conservative for Gabagool)
    max_loss_per_trade: float = 20.0  # Max $ loss on single trade

    # Error limits
    max_consecutive_errors: int = 5  # Halt after N consecutive failures
    max_errors_per_hour: int = 20  # Halt if too many errors in 1 hour

    # Timing
    cooldown_seconds: int = 300  # 5 minute cooldown after halt

    # Auto-recovery
    auto_recover: bool = True  # Automatically resume after cooldown
    require_manual_reset: bool = False  # Require human intervention to resume


@dataclass
class CircuitBreakerState:
    """Mutable state tracked by circuit breaker."""

    is_halted: bool = False
    halt_reason: str = ""
    halt_time: Optional[datetime] = None

    # Position tracking
    positions_by_market: Dict[str, float] = field(default_factory=dict)
    total_position: float = 0.0

    # P&L tracking
    daily_pnl: float = 0.0
    daily_pnl_reset_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Error tracking
    consecutive_errors: int = 0
    errors_this_hour: int = 0
    error_hour_start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Metrics
    total_trades: int = 0
    successful_trades: int = 0
    failed_trades: int = 0


class CircuitBreaker:
    """
    Circuit breaker for Gabagool strategy.

    Monitors trading activity and automatically halts execution when:
    - Position limits exceeded
    - Daily loss limit hit
    - Too many consecutive errors
    - Anomalous market conditions detected

    Ported from Rust reference: src/circuit_breaker.rs
    """

    def __init__(self, config: CircuitBreakerConfig = None):
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitBreakerState()
        self._lock = asyncio.Lock()

    async def check_can_trade(self, market_id: str, size: float) -> Tuple[bool, str]:
        """
        Check if a trade is allowed under current circuit breaker state.

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        async with self._lock:
            # Check if halted
            if self.state.is_halted:
                # Check if cooldown has passed
                if self._should_auto_recover():
                    await self._reset()
                else:
                    return False, f"Circuit breaker halted: {self.state.halt_reason}"

            # Reset daily P&L at midnight UTC
            self._maybe_reset_daily_pnl()

            # Reset hourly error count
            self._maybe_reset_hourly_errors()

            # Check position limits
            current_market_position = self.state.positions_by_market.get(market_id, 0.0)
            new_market_position = current_market_position + size

            if new_market_position > self.config.max_position_per_market:
                return (
                    False,
                    f"Would exceed market position limit: ${new_market_position:.2f} > ${self.config.max_position_per_market:.2f}",
                )

            new_total_position = self.state.total_position + size
            if new_total_position > self.config.max_total_position:
                return (
                    False,
                    f"Would exceed total position limit: ${new_total_position:.2f} > ${self.config.max_total_position:.2f}",
                )

            # Check daily loss limit
            if self.state.daily_pnl < -self.config.max_daily_loss:
                await self._halt(
                    f"Daily loss limit exceeded: ${-self.state.daily_pnl:.2f}"
                )
                return False, self.state.halt_reason

            return True, "OK"

    async def record_trade_result(
        self,
        market_id: str,
        size: float,
        pnl: float,
        success: bool,
        error_msg: str = "",
    ):
        """
        Record the result of a trade attempt.

        Updates position tracking, P&L, and error counts.
        May trigger circuit breaker halt if thresholds exceeded.
        """
        async with self._lock:
            self.state.total_trades += 1

            if success:
                self.state.successful_trades += 1
                self.state.consecutive_errors = 0

                # Update positions
                current = self.state.positions_by_market.get(market_id, 0.0)
                self.state.positions_by_market[market_id] = current + size
                self.state.total_position += size

                # Update P&L
                self.state.daily_pnl += pnl

                # Check for anomalous loss on "successful" trade
                if pnl < -self.config.max_loss_per_trade:
                    await self._halt(f"Anomalous loss on trade: ${-pnl:.2f}")
            else:
                self.state.failed_trades += 1
                self.state.consecutive_errors += 1
                self.state.errors_this_hour += 1

                if error_msg:
                    logger.warning(f"Trade failed: {error_msg}")

                # Check consecutive error limit
                if self.state.consecutive_errors >= self.config.max_consecutive_errors:
                    await self._halt(
                        f"Too many consecutive errors: {self.state.consecutive_errors}"
                    )

                # Check hourly error limit
                if self.state.errors_this_hour >= self.config.max_errors_per_hour:
                    await self._halt(
                        f"Too many errors this hour: {self.state.errors_this_hour}"
                    )

    async def record_position_closed(self, market_id: str, size: float, pnl: float):
        """Record when a position is closed (merged or resolved)."""
        async with self._lock:
            current = self.state.positions_by_market.get(market_id, 0.0)
            self.state.positions_by_market[market_id] = max(0, current - size)
            self.state.total_position = max(0, self.state.total_position - size)
            self.state.daily_pnl += pnl

    async def force_halt(self, reason: str):
        """Manually halt the circuit breaker."""
        async with self._lock:
            await self._halt(reason)

    async def manual_reset(self):
        """Manually reset the circuit breaker (for admin use)."""
        async with self._lock:
            await self._reset()
            logger.info("Circuit breaker manually reset")

    async def _halt(self, reason: str):
        """Halt the circuit breaker."""
        self.state.is_halted = True
        self.state.halt_reason = reason
        self.state.halt_time = datetime.now(timezone.utc)

        logger.critical(f"CIRCUIT BREAKER HALTED: {reason}")

    async def _reset(self):
        """Reset the circuit breaker after cooldown."""
        self.state.is_halted = False
        self.state.halt_reason = ""
        self.state.halt_time = None
        self.state.consecutive_errors = 0
        logger.info("Circuit breaker reset - trading resumed")

    def _should_auto_recover(self) -> bool:
        """Check if we should auto-recover from halt."""
        if not self.config.auto_recover:
            return False
        if self.config.require_manual_reset:
            return False
        if self.state.halt_time is None:
            return False

        elapsed = (datetime.now(timezone.utc) - self.state.halt_time).total_seconds()
        return elapsed >= self.config.cooldown_seconds

    def _maybe_reset_daily_pnl(self):
        """Reset daily P&L at midnight UTC."""
        now = datetime.now(timezone.utc)
        if now.date() > self.state.daily_pnl_reset_time.date():
            self.state.daily_pnl = 0.0
            self.state.daily_pnl_reset_time = now
            logger.info("Daily P&L reset at midnight UTC")

    def _maybe_reset_hourly_errors(self):
        """Reset hourly error count."""
        now = datetime.now(timezone.utc)
        elapsed = (now - self.state.error_hour_start).total_seconds()
        if elapsed >= 3600:
            self.state.errors_this_hour = 0
            self.state.error_hour_start = now

    def get_status(self) -> dict:
        """Get current circuit breaker status for monitoring."""
        return {
            "is_halted": self.state.is_halted,
            "halt_reason": self.state.halt_reason,
            "total_position": self.state.total_position,
            "positions_by_market": dict(self.state.positions_by_market),
            "daily_pnl": self.state.daily_pnl,
            "consecutive_errors": self.state.consecutive_errors,
            "errors_this_hour": self.state.errors_this_hour,
            "total_trades": self.state.total_trades,
            "successful_trades": self.state.successful_trades,
            "failed_trades": self.state.failed_trades,
            "success_rate": (
                self.state.successful_trades / max(1, self.state.total_trades) * 100
            ),
        }
