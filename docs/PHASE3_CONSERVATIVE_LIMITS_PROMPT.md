# Phase 3: Conservative Buy Limit Checks

## Context

Phase 2 implemented dual tracking with `confirmed_size` and `pending_fills`. Currently:
- **Buy limits** use `confirmed_size + pending_orders`
- **Sell availability** uses `effective_size` (confirmed + pending_fill_buys - pending_fill_sells)

This creates a gap: if multiple WS buy fills arrive before API sync, we could exceed position limits because `pending_fill_buys` aren't counted in buy limit checks.

## Problem Example

```
Config: max_position_per_market = 100

Timeline:
1. API sync: confirmed_size = 80
2. WS fill: BUY 15 -> pending_fill_buys = 15, effective = 95
3. WS fill: BUY 15 -> pending_fill_buys = 30, effective = 110
4. Bot checks can_buy(10): confirmed(80) + pending_orders(0) = 80 < 100 ✓
5. Bot places BUY 10 -> actual exposure could reach 120!

Problem: We're checking against 80 when actual exposure is 110+
```

## Solution: Conservative Exposure Formula

For buy limit checks, use:
```python
conservative_exposure = confirmed_size + pending_fill_buys + pending_order_buys
```

This counts ALL potential buy-side exposure:
- `confirmed_size`: What API says we have
- `pending_fill_buys`: WS buy fills not yet confirmed (will likely settle)
- `pending_order_buys`: Open buy orders (may fill)

Note: We intentionally DON'T subtract `pending_fill_sells` for buy limits because:
1. Sells might not settle
2. Being conservative means assuming worst case (all buys settle, no sells settle)

## Implementation Changes

### 1. Update `check_limits()` in `inventory_manager.py`

Current (~line 447):
```python
confirmed = position.confirmed_size
effective_position = confirmed + pending_orders
```

Change to:
```python
confirmed = position.confirmed_size
pending_fill_buys = position.pending_fill_buys
conservative_exposure = confirmed + pending_fill_buys + pending_orders
```

Update the limit check:
```python
if conservative_exposure >= self.config.max_position_per_market:
    limits.can_buy = False
    limits.buy_limit_reason = (
        f"Position {confirmed:.0f} + pending_fills {pending_fill_buys:.0f} + "
        f"pending_orders {pending_orders:.0f} >= max {self.config.max_position_per_market}"
    )
```

### 2. Update `can_place_order()` in `inventory_manager.py`

Current (~line 499):
```python
confirmed = position.confirmed_size
projected_size = confirmed + pending_orders + size
```

Change to:
```python
confirmed = position.confirmed_size
pending_fill_buys = position.pending_fill_buys
projected_size = confirmed + pending_fill_buys + pending_orders + size
```

Update the error message to include all components.

### 3. Update `get_adjusted_order_size()` in `inventory_manager.py`

Current (~line 548):
```python
confirmed = position.confirmed_size
pending_orders = self.get_pending_buy_size(token_id)
effective_position = confirmed + pending_orders
remaining_capacity = self.config.max_position_per_market - effective_position
```

Change to:
```python
confirmed = position.confirmed_size
pending_fill_buys = position.pending_fill_buys
pending_orders = self.get_pending_buy_size(token_id)
conservative_exposure = confirmed + pending_fill_buys + pending_orders
remaining_capacity = self.config.max_position_per_market - conservative_exposure
```

### 4. Update `calculate_liability()` (Optional Enhancement)

Consider whether liability should also use conservative exposure. Current implementation uses `confirmed_size`. Options:
- Keep as-is (liability = confirmed exposure, conservative for display)
- Add separate `calculate_conservative_liability()` for limit checks
- Update to use `confirmed + pending_fill_buys`

Recommendation: Keep liability calculation as-is for now. Liability is about worst-case loss on confirmed positions. Pending fills have their own risk characteristics.

## Sell Limits - No Change

Sell availability should continue to use `effective_size`:
```python
effective = position.effective_size  # confirmed + pending_buys - pending_sells
if effective <= 0:
    limits.can_sell = False
```

This is correct because:
1. We want to allow quick exits after WS buy fills
2. If we have pending sells, available inventory is reduced
3. Sells reduce risk, so being permissive is appropriate

## Test Requirements

### Required Tests

1. `test_buy_limits_include_pending_fill_buys`
   - Create pending buy fills via `update_from_fill()`
   - Verify `check_limits()` blocks buys when `confirmed + pending_fill_buys + pending_orders >= max`

2. `test_can_place_order_includes_pending_fill_buys`
   - Similar to above but for `can_place_order()`

3. `test_adjusted_order_size_respects_pending_fills`
   - Verify `get_adjusted_order_size()` returns reduced size accounting for pending fills

4. `test_sell_limits_unchanged`
   - Verify sell availability still uses `effective_size`
   - Confirm sells allowed even when `confirmed_size = 0` but `pending_fill_buys > 0`

5. `test_conservative_exposure_calculation`
   - Verify formula: `conservative = confirmed + pending_fill_buys + pending_orders`
   - Test with various combinations

### Example Test

```python
def test_buy_limits_include_pending_fill_buys(self, manager):
    """Buy limits should use conservative exposure including pending fill buys."""
    # Set confirmed at 70
    manager.set_position("token1", size=70.0, avg_entry_price=0.50)

    # Add pending buy fill of 25 (effective = 95, conservative = 95)
    fill = Fill(
        order_id="order1",
        token_id="token1",
        side=OrderSide.BUY,
        price=0.50,
        size=25.0,
        trade_id="fill1",
    )
    manager.update_from_fill(fill)

    pos = manager.get_position("token1")
    assert pos.confirmed_size == 70.0
    assert pos.pending_fill_buys == 25.0
    assert pos.effective_size == 95.0

    # Should block buys: 70 + 25 + 0 = 95, adding 10 would = 105 > 100
    limits = manager.check_limits("token1")
    assert limits.can_buy is True  # Can still buy small amounts

    allowed, reason = manager.can_place_order("token1", OrderSide.BUY, 10)
    assert allowed is False
    assert "pending_fills" in reason or "95" in reason
```

## Migration Notes

1. This is a behavioral change - buys will be blocked more aggressively
2. Existing positions won't be affected (only new buy attempts)
3. No data migration needed
4. Consider adding logging when pending_fill_buys causes a block (helps debugging)

## Success Criteria

1. Buy limit checks include pending fill buys in calculation
2. Cannot exceed `max_position_per_market` even with rapid WS fills
3. Sell availability unchanged (still uses effective_size)
4. Clear log messages showing all components of conservative exposure
5. All existing tests pass + new tests for Phase 3 behavior

## Files to Modify

- `rebates/active_quoting/inventory_manager.py` - Core limit check logic
- `tests/unit/active_quoting/test_inventory_manager.py` - New tests
- `docs/INVENTORY_TRACKING_PLAN.md` - Mark Phase 3 complete
