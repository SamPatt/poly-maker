# Inventory Tracking Improvement Plan (Revised)

## Problem Statement

The bot's internal position tracking diverges from on-chain reality because:
1. **Fill protection blocks API updates** for 60s after any fill (the root cause)
2. WebSocket fills are treated as authoritative over API
3. No mechanism detects or corrects systematic drift
4. Position limits are checked against wrong values

## Key Insight

The 60-second "fill protection" window in `inventory_manager.py` is the direct cause of drift.
When phantom/unsettled fills come in, they inflate internal position AND block API from correcting it.

## Three Distinct Concepts (Must Not Collide)

1. **confirmed_size**: Last known position from API snapshot
2. **pending_fill_delta**: WS fills not yet confirmed by API (tracked by trade_id)
3. **pending_order_buys**: Reserved capacity for open buy orders (existing system)

These are SEPARATE values that must be tracked independently.

## Position Calculations

```python
# For display and PnL
effective_size = confirmed_size + pending_fill_buys - pending_fill_sells

# For buy limit checks (conservative - assumes all buys settle)
conservative_exposure = confirmed_size + pending_fill_buys + pending_order_buys

# For sell limit checks
available_to_sell = confirmed_size + pending_fill_buys - pending_fill_sells
```

## Implementation Details

### 1. New Position Model

```python
@dataclass
class PendingFill:
    """Track individual pending fills for precise reconciliation."""
    trade_id: str
    side: OrderSide
    size: float
    price: float
    timestamp: datetime

@dataclass
class TrackedPosition:
    token_id: str
    confirmed_size: float = 0.0           # From API
    confirmed_at: Optional[datetime] = None
    pending_fills: Dict[str, PendingFill] = field(default_factory=dict)  # trade_id -> fill

    @property
    def pending_fill_buys(self) -> float:
        return sum(f.size for f in self.pending_fills.values() if f.side == OrderSide.BUY)

    @property
    def pending_fill_sells(self) -> float:
        return sum(f.size for f in self.pending_fills.values() if f.side == OrderSide.SELL)

    @property
    def effective_size(self) -> float:
        return self.confirmed_size + self.pending_fill_buys - self.pending_fill_sells
```

### 2. Fill Handling (WS fill arrives)

```python
def on_fill(self, fill: Fill):
    position = self.get_position(fill.token_id)

    # Track as pending (not confirmed yet)
    position.pending_fills[fill.trade_id] = PendingFill(
        trade_id=fill.trade_id,
        side=fill.side,
        size=fill.size,
        price=fill.price,
        timestamp=datetime.utcnow(),
    )

    # Update PnL/risk using effective_size (not confirmed_size)
    self._update_pnl(fill, position.effective_size)
```

### 3. API Sync (Always runs, no blocking)

```python
def sync_from_api(self, token_id: str, api_size: float):
    position = self.get_position(token_id)
    old_confirmed = position.confirmed_size
    old_effective = position.effective_size

    # Calculate expected position if all pending fills settled
    expected = old_confirmed + position.pending_fill_buys - position.pending_fill_sells

    # Update confirmed to API value
    position.confirmed_size = api_size
    position.confirmed_at = datetime.utcnow()

    # Partial reconciliation: calculate how much API "absorbed"
    absorbed = api_size - old_confirmed

    if abs(absorbed) < 0.01:
        # No change in API - pending fills not yet reflected
        # Keep pending_fills as-is
        pass
    elif abs(api_size - expected) < 0.01:
        # API matches expectation - all pending fills confirmed
        position.pending_fills.clear()
        logger.info(f"All fills confirmed for {token_id[:20]}...: {api_size:.2f}")
    else:
        # Partial confirmation or discrepancy
        # Age out old pending fills (> 30s) since API should have them by now
        cutoff = datetime.utcnow() - timedelta(seconds=30)
        old_fills = {k: v for k, v in position.pending_fills.items() if v.timestamp < cutoff}

        if old_fills:
            old_delta = sum(
                f.size if f.side == OrderSide.BUY else -f.size
                for f in old_fills.values()
            )
            logger.warning(
                f"Aging out {len(old_fills)} pending fills for {token_id[:20]}... "
                f"(delta={old_delta:.2f}, api={api_size:.2f})"
            )
            for k in old_fills:
                del position.pending_fills[k]
```

### 4. Position Limit Checks

```python
def can_buy(self, token_id: str, size: float) -> bool:
    position = self.get_position(token_id)
    pending_orders = self.get_pending_order_buys(token_id)  # Existing system

    # Conservative exposure: confirmed + pending fill buys + pending order buys
    conservative = (
        position.confirmed_size +
        position.pending_fill_buys +
        pending_orders
    )

    projected = conservative + size

    if projected > self.config.max_position_per_market:
        logger.warning(
            f"Buy blocked for {token_id[:20]}...: "
            f"confirmed={position.confirmed_size:.0f} + "
            f"pending_fills={position.pending_fill_buys:.0f} + "
            f"pending_orders={pending_orders:.0f} + "
            f"new={size:.0f} = {projected:.0f} > {self.config.max_position_per_market}"
        )
        return False
    return True
```

### 5. Force Reconciliation (On WS gaps or periodic)

```python
def force_reconcile(self, token_id: str, api_size: float):
    """Called on WS reconnect, gaps, or periodic hard sync."""
    position = self.get_position(token_id)

    discrepancy = position.effective_size - api_size

    if abs(discrepancy) > 5.0:
        logger.error(
            f"FORCE RECONCILE {token_id[:20]}...: "
            f"effective={position.effective_size:.2f} vs api={api_size:.2f} "
            f"(gap={discrepancy:.2f})"
        )

    # Trust API, clear all pending
    position.confirmed_size = api_size
    position.pending_fills.clear()
    position.confirmed_at = datetime.utcnow()
```

## Migration Path

### Phase 1: Remove Fill Protection + Add Logging (IMMEDIATE) ✅ COMPLETED
- ✅ Remove `has_recent_fill()` checks that block API sync
- ✅ Remove `FILL_PROTECTION_SECONDS` constant
- ✅ Remove pending_buys blocking that caused sync deadlock
- ✅ Add logging to show discrepancies between internal and API
- **This alone should fix the drift issue**

### Phase 2: Implement Dual Tracking ✅ COMPLETED
- ✅ Add `PendingFill` and `TrackedPosition` dataclasses
- ✅ Track fills by trade_id for precise reconciliation (with synthesized fallback)
- ✅ `update_from_fill()` adds to pending_fills instead of updating confirmed_size
- ✅ `set_position()` updates confirmed_size and reconciles pending fills (oldest-first)
- ✅ Age out stale pending fills after 30s with detailed logging
- ✅ Force reconcile all positions on WebSocket disconnect
- ✅ All consumers use `effective_size` via `.size` property alias
- ✅ Comprehensive tests for dual tracking behavior (673 tests passing)

### Phase 3: Conservative Buy Limit Checks ✅ COMPLETED
- ✅ `check_limits()` uses conservative_exposure for buy limits
- ✅ `can_place_order()` uses conservative_exposure for buy checks
- ✅ `get_adjusted_order_size()` uses conservative_exposure for buy sizing
- ✅ Sell limits unchanged (still use effective_size for quick exits)
- ✅ Comprehensive tests for Phase 3 behavior (77 tests passing)

**Conservative exposure formula:**
```python
conservative_exposure = confirmed + pending_fill_buys + pending_order_buys
```

This prevents exceeding position limits even when multiple WS fills arrive
before API sync. Pending sell fills are intentionally NOT credited (worst case
assumption: all buys settle, no sells settle).

### Phase 4: Update PnL/Risk ✅ COMPLETED (as part of Phase 2)
- ✅ Risk manager uses `effective_size` (via `.size` alias)
- ✅ PnL tracker uses `effective_size` (via `.size` alias)
- ✅ Alerts use `effective_size` (via `.size` alias)
- ✅ `get_summary()` includes both `confirmed_size` and `effective_size`

## Critical Fix: Remove Fill Protection

The most important change is removing the fill protection in `inventory_manager.py`:

```python
# DELETE THIS (lines 114-130):
def has_recent_fill(self, token_id: str, seconds: float = FILL_PROTECTION_SECONDS) -> bool:
    ...

# DELETE THIS (line 20-21):
FILL_PROTECTION_SECONDS = 60.0
```

And in `bot.py`, remove the checks that use it (lines 329-353).

## Success Metrics

1. `confirmed_size` always matches API within seconds
2. Position limits never exceeded (checked against conservative exposure)
3. Discrepancy alerts are actionable (show pending vs confirmed)
4. No "phantom fill" drift lasting more than 30 seconds
