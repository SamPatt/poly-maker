# Order Tracking & Limit Compliance Fix Plan

## Status: COMPLETE

All 6 phases implemented as of 2025-01-14.

## Problem Summary

The AQ bot was not properly obeying position limits because orders/fills were not being tracked correctly. Key issues:

1. **Critical**: `_on_order_update` only logged; pending orders never reconciled with WS updates
2. **High**: Pending buy reservations cleared optimistically without exchange confirmation
3. **High**: Blocking REST API call in async loop; 30s stale protection insufficient
4. **Medium**: REST reconciliation helper exists but never called
5. **Medium**: Local DB is persistence-only, not authoritative ledger

## Implementation Phases

### Phase 1: Fix Order Update Handler - COMPLETED
**Status**: Done (2025-01-14)

**Files Changed**:
- `rebates/active_quoting/bot.py` - Added OrderStatus import, rewrote `_on_order_update()`

**Changes**:
- Sync order_manager state with authoritative user channel state
- Release pending buy reservations on CONFIRMED terminal states (CANCELLED, EXPIRED, REJECTED)
- Only release for BUY orders, only release remaining_size

**Tests Added**: 11 new tests in `tests/unit/active_quoting/test_bot.py`

---

### Phase 2: Fix Pending Buy Reservation Lifecycle - COMPLETED
**Status**: Done (2025-01-14)

**Problem**: Reservations were cleared optimistically when calling `cancel_all_for_token()` without waiting for exchange confirmation.

**Files Changed**:
- `rebates/active_quoting/bot.py` - Removed optimistic clearing in `_cancel_market_quotes()`

**Changes**:
1. Removed `clear_pending_buys()` call from `_cancel_market_quotes()`
2. Reservations now released via `_on_order_update()` when CANCELLED confirmed

**Tests Added**: Tests for non-optimistic cancel behavior

---

### Phase 3: Fix API Position Sync - COMPLETED
**Status**: Done (2025-01-14)

**Problem**:
- Used blocking `requests.get()` in async event loop
- 30s protection window was not enough

**Files Changed**:
- `rebates/active_quoting/bot.py` - `_sync_positions_from_api()`

**Changes**:
1. Converted to async HTTP client (aiohttp)
2. Increased fill protection window to 60s
3. Only allow API to reduce position if no pending buys AND no recent fills

---

### Phase 4: Add Periodic Reconciliation Loop - COMPLETED
**Status**: Done (2025-01-14)

**Problem**: `reconcile_with_api_orders()` existed in UserChannelManager but was never called.

**Files Changed**:
- `rebates/active_quoting/bot.py` - Added `_reconcile_orders()` method

**Changes**:
1. Call reconciliation on startup after WS connect
2. Periodic reconciliation every 60s in main loop
3. Reconcile pending buy reservations after order reconciliation
4. Immediate reconciliation when WebSocket gaps detected

---

### Phase 5: Add Event Ledger - COMPLETED
**Status**: Done (2025-01-14)

**Purpose**: Create append-only event log for fills and order updates for gap detection and recovery.

**Files Added**:
- `rebates/active_quoting/event_ledger.py` - EventLedger class with SQLite persistence

**Features**:
- Append-only event logging with sequence numbers
- WebSocket sequence gap detection
- Gap tracking and resolution
- Event retrieval by type, token, time range

**Tests Added**: `tests/unit/active_quoting/test_event_ledger.py`

---

### Phase 6: Add Safety Halts - COMPLETED
**Status**: Done (2025-01-14)

**Purpose**: Detect WS message gaps and halt quoting until reconciled.

**Files Changed**:
- `rebates/active_quoting/config.py` - Added safety halt config options
- `rebates/active_quoting/risk_manager.py` - Added HaltReason enum and WS gap halt methods
- `rebates/active_quoting/bot.py` - Added safety halt logic in reconciliation and main loop
- `rebates/active_quoting/alerts.py` - Added halt_reason to circuit breaker alerts

**Config Options**:
- `halt_on_ws_gaps: bool = True` - Enable/disable safety halts
- `ws_gap_reconcile_attempts: int = 3` - Max reconciliation attempts before halting
- `ws_gap_recovery_interval_seconds: float = 30.0` - Recovery attempt interval

**Features**:
- Track reconciliation attempts when gaps persist
- Trigger safety halt after max attempts via `HaltReason.WS_GAP_UNRESOLVED`
- Periodic recovery attempts when halted due to gaps
- Auto-recovery when gaps are resolved
- Telegram alerts include halt reason

**Tests Added**: 17 new tests for HaltReason in `tests/unit/active_quoting/test_risk_manager.py`

---

## Progress Tracking

- [x] Phase 1: Fix _on_order_update handler
- [x] Phase 2: Fix pending buy reservation lifecycle
- [x] Phase 3: Fix API position sync
- [x] Phase 4: Add periodic reconciliation loop
- [x] Phase 5: Add event ledger
- [x] Phase 6: Add safety halts

## Test Coverage

All 653 tests pass as of completion.
