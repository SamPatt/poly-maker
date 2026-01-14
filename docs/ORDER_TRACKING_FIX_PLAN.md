# Order Tracking & Limit Compliance Fix Plan

## Problem Summary

The AQ bot is not properly obeying position limits because orders/fills are not being tracked correctly. Key issues:

1. **Critical**: `_on_order_update` only logged; pending orders never reconciled with WS updates
2. **High**: Pending buy reservations cleared optimistically without exchange confirmation
3. **High**: Blocking REST API call in async loop; 30s stale protection insufficient
4. **Medium**: REST reconciliation helper exists but never called
5. **Medium**: Local DB is persistence-only, not authoritative ledger

## Implementation Phases

### Phase 1: Fix Order Update Handler - COMPLETED
**Status**: Done (2024-01-14)

**Files Changed**:
- `rebates/active_quoting/bot.py` - Added OrderStatus import, rewrote `_on_order_update()`

**Changes**:
- Sync order_manager state with authoritative user channel state
- Release pending buy reservations on CONFIRMED terminal states (CANCELLED, EXPIRED, REJECTED)
- Only release for BUY orders, only release remaining_size

**Tests Added**: 11 new tests in `tests/unit/active_quoting/test_bot.py`

---

### Phase 2: Fix Pending Buy Reservation Lifecycle - COMPLETED
**Status**: In Progress

**Problem**: Reservations are cleared optimistically when calling `cancel_all_for_token()` without waiting for exchange confirmation.

**Files to Change**:
- `rebates/active_quoting/bot.py` - Remove optimistic clearing in `_cancel_market_quotes()`

**Changes Needed**:
1. Remove `clear_pending_buys()` call from `_cancel_market_quotes()` 
2. Reservations will now be released via `_on_order_update()` when CANCELLED confirmed

**Tests to Add**:
- Test that cancel does NOT clear pending buys immediately
- Test that pending buys are released when CANCELLED confirmation arrives

---

### Phase 3: Fix API Position Sync - COMPLETED
**Status**: Not Started

**Problem**: 
- Uses blocking `requests.get()` in async event loop
- 30s protection window may not be enough

**Files to Change**:
- `rebates/active_quoting/bot.py` - `_sync_positions_from_api()`

**Changes Needed**:
1. Convert to async HTTP client (aiohttp)
2. Increase fill protection window to 60s
3. Only allow API to reduce position if no pending buys AND no open orders for token

---

### Phase 4: Add Periodic Reconciliation Loop - TODO
**Status**: Not Started

**Problem**: `reconcile_with_api_orders()` exists in UserChannelManager but is never called.

**Files to Change**:
- `rebates/active_quoting/bot.py` - Add `_reconcile_orders()` method, call from main loop

**Changes Needed**:
1. Call reconciliation on startup after WS connect
2. Add periodic reconciliation every 60s in main loop
3. Reconcile pending buy reservations after order reconciliation

---

### Phase 5: Add Event Ledger (Optional) - TODO
**Status**: Not Started (Can defer)

**Purpose**: Create append-only event log for fills and order updates for gap detection and recovery.

---

### Phase 6: Add Safety Halts - TODO
**Status**: Not Started

**Purpose**: Detect WS message gaps and halt quoting until reconciled.

---

## Progress Tracking

- [x] Phase 1: Fix _on_order_update handler
- [x] Phase 2: Fix pending buy reservation lifecycle
- [x] Phase 3: Fix API position sync  
- [ ] Phase 4: Add periodic reconciliation loop
- [ ] Phase 5: Add event ledger (optional)
- [ ] Phase 6: Add safety halts
