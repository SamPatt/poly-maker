"""
Gabagool Strategy Module

Implements the paired position arbitrage strategy for Polymarket 15-minute crypto markets.
When YES + NO prices sum to less than $1.00, buying both guarantees profit at settlement.
"""

from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerState
from .scanner import GabagoolScanner, Opportunity
from .monitor import GabagoolMonitor
from . import config

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerState",
    "GabagoolScanner",
    "Opportunity",
    "GabagoolMonitor",
    "config",
]
