# Phase 2: Implement Dual Tracking for Inventory Manager

## Context
Phase 1 removed the fill protection blocking that caused position drift. Now we need proper dual tracking to distinguish between confirmed (API) positions and pending (WebSocket) fills.

## Goal
Refactor `InventoryManager` to track positions using two separate concepts:
1. **confirmed_size**: Last known position from API snapshot
2. **pending_fills**: WebSocket fills not yet confirmed by API (tracked by trade_id)

## Critical Migration Note
`InventoryManager.get_position()` will now return a `TrackedPosition` object. **All code currently using `.size` must be updated to use `.effective_size`** until Phase 3 updates limit checks. Failing to do this will under-report risk and cause position skew.

Consumers that need updating:
- **Risk manager**: `rebates/active_quoting/risk_manager.py` - position exposure calculations
- **PnL tracker**: `rebates/active_quoting/pnl_tracker.py` - realized/unrealized PnL
- **Alerts**: `rebates/active_quoting/alerts.py` - position size in fill alerts
- **Bot**: `rebates/active_quoting/bot.py` - anywhere `get_position().size` is called

## Implementation Tasks

### 1. Add new data structures in `rebates/active_quoting/inventory_manager.py`:
```python
@dataclass
class PendingFill:
    trade_id: str  # Primary key for reconciliation
    side: OrderSide
    size: float
    price: float
    timestamp: datetime

@dataclass
class TrackedPosition:
    token_id: str
    confirmed_size: float = 0.0
    confirmed_at: Optional[datetime] = None
    pending_fills: Dict[str, PendingFill] = field(default_factory=dict)

    @property
    def pending_fill_buys(self) -> float: ...
    @property
    def pending_fill_sells(self) -> float: ...
    @property
    def effective_size(self) -> float:
        return self.confirmed_size + self.pending_fill_buys - self.pending_fill_sells
```

### 2. Handle missing trade_id:
- Some fills may arrive without a trade_id
- Synthesize a fallback key: `f"{order_id}_{timestamp_ms}"` or `f"fill_{timestamp_ms}_{size}"`
- This prevents collapsing multiple fills into one entry

### 3. Update fill handling:
- When WS fill arrives, add to `pending_fills` by trade_id (don't update confirmed_size)
- Use synthesized key if trade_id is missing
- Log the trade_id used for traceability

### 4. Implement partial confirmation reconciliation:
- When API sync runs, calculate `absorbed = api_size - old_confirmed`
- If API moved by +N but pending fills sum to +M:
  - Reduce pending fills by N using **oldest-first** removal
  - Don't do full clear unless API matches exactly
  - Don't rely only on age-out
```python
def _reconcile_pending_fills(self, position: TrackedPosition, absorbed: float):
    """Remove pending fills that API has absorbed, oldest first."""
    if abs(absorbed) < 0.01:
        return  # No change

    remaining_to_absorb = absorbed
    fills_by_age = sorted(position.pending_fills.values(), key=lambda f: f.timestamp)

    for fill in fills_by_age:
        fill_delta = fill.size if fill.side == OrderSide.BUY else -fill.size
        if sign(fill_delta) == sign(remaining_to_absorb):
            if abs(fill_delta) <= abs(remaining_to_absorb):
                # Fully absorbed
                del position.pending_fills[fill.trade_id]
                remaining_to_absorb -= fill_delta
            # else: partial - keep for now
```

### 5. Age-out with detailed logging:
- Pending fills older than 30s that weren't absorbed should be aged out
- **Log specific trade_ids being removed and net delta**:
```python
logger.warning(
    f"Aging out pending fills for {token_id[:20]}...: "
    f"trade_ids={list(old_fills.keys())}, net_delta={net_delta:+.2f}, "
    f"confirmed={position.confirmed_size:.2f}"
)
```

### 6. Add force reconciliation:
- On WS reconnect/gaps: trust API, clear all pending_fills
- Log discrepancies before clearing

### 7. Update all consumers of `.size` to use `.effective_size`:
- Search for `get_position(` and update all `.size` references
- This is critical to avoid under-reporting

## Required Tests
Add to `tests/unit/active_quoting/test_inventory_manager.py`:

1. **test_partial_confirmation_removes_oldest_fills_first**: API absorbs 50 of 80 pending, verify oldest fills removed, newest kept
2. **test_age_out_logs_trade_ids_and_delta**: Fills older than 30s are removed with proper logging
3. **test_missing_trade_id_uses_synthesized_key**: Fill without trade_id gets unique key, multiple such fills don't collide
4. **test_effective_size_used_by_consumers**: Verify risk manager/PnL use effective_size not confirmed_size

## Key Formulas
```python
effective_size = confirmed_size + pending_fill_buys - pending_fill_sells
```

## Reference
See `docs/INVENTORY_TRACKING_PLAN.md` sections 1-3 for additional code examples.

## Important
- Keep existing `pending_order_buys` system separate (for open buy orders)
- Don't change position limit check formulas yet (that's Phase 3)
- Run tests after changes: `pytest tests/unit/active_quoting/ -v`
