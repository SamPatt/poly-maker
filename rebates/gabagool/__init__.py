"""
Gabagool Strategy Module

Implements the paired position arbitrage strategy for Polymarket 15-minute crypto markets.
When YES + NO prices sum to less than $1.00, buying both guarantees profit at settlement.
"""

from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerState
from .scanner import GabagoolScanner, Opportunity
from .monitor import GabagoolMonitor
from .executor import GabagoolExecutor, ExecutionResult, TrackedPosition, ExecutionStrategy
from .reconciler import PositionReconciler, PositionStatus, ReconciliationResult
from .position_manager import PositionManager, PositionSummary
from . import config

__all__ = [
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerState",
    # Scanner
    "GabagoolScanner",
    "Opportunity",
    # Monitor
    "GabagoolMonitor",
    # Executor
    "GabagoolExecutor",
    "ExecutionResult",
    "TrackedPosition",
    "ExecutionStrategy",
    # Reconciler
    "PositionReconciler",
    "PositionStatus",
    "ReconciliationResult",
    # Position Manager
    "PositionManager",
    "PositionSummary",
    # Config
    "config",
]
