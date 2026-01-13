# Active Two-Sided Quoting Strategy Research

## Implementation Status

| Phase | Description | Status | Tests |
|-------|-------------|--------|-------|
| 1 | Foundation + WebSocket Connectivity | **COMPLETE** | 142 unit, 5 integration |
| 2 | Quote Engine + Order Management | **COMPLETE** | 85 unit, 8 integration |
| 3 | Inventory + Momentum Protection | **COMPLETE** | 88 unit, 10 integration |
| 4 | Risk Management + Circuit Breaker | **COMPLETE** | 66 unit, 16 integration |
| 5 | Full Bot + Analytics | **COMPLETE** | 84 unit, 18 integration |
| 6 | Production Hardening | Not Started | - |

### Phase 1 Completed Components
- `rebates/active_quoting/config.py` - ActiveQuotingConfig with all parameters
- `rebates/active_quoting/models.py` - Quote, OrderbookState, MomentumState, Fill, OrderState, Position, MarketState
- `rebates/active_quoting/orderbook_manager.py` - Real-time orderbook via market WebSocket
- `rebates/active_quoting/user_channel_manager.py` - Authenticated order/fill state via user WebSocket
- Integration gate passed: Both WebSockets stable for 5 min (56 updates, 0 disconnects)

### Phase 2 Completed Components
- `rebates/active_quoting/quote_engine.py` - Dynamic quote pricing with:
  - Quote at best_bid/best_ask (offset 0 by default)
  - Improve by 1 tick only when spread >= IMPROVE_WHEN_SPREAD_TICKS
  - Inventory skew based on position (skew = coefficient × inventory)
  - Hysteresis: only refresh if quote is >= REFRESH_THRESHOLD_TICKS from target
  - Clamping to prevent crossing spread
- `rebates/active_quoting/order_manager.py` - Order placement with:
  - feeRateBps handling (fetch via GET /fee-rate?token_id=...)
  - Post-only flag enforcement
  - Batch order placement (up to 15 orders per request)
  - Cancel/replace logic with hysteresis
  - Dry-run mode for safe testing
- Unit tests: 37 quote_engine, 34 order_manager, 14 quote_cycle (total: 85)
- Integration tests: 8 tests in test_order_lifecycle.py
  - Fee rate fetching from API ✓
  - Fee rate caching ✓
  - Order placement (dry-run) ✓
  - Order cancellation (dry-run) ✓
  - Quote calculation from live orderbook ✓
  - Full quote cycle with live data ✓
  - User channel connectivity ✓
  - **Live order placement and cancellation ✓** (real orders on Polymarket)

### Phase 3 Completed Components
- `rebates/active_quoting/inventory_manager.py` - Position tracking and skewing with:
  - Position tracking per market (authoritative from UserChannelManager fills)
  - Liability calculation (worst-case loss = shares × entry_price)
  - Skew factor calculation based on position (skew = coefficient × inventory)
  - Hard position limits enforcement (MAX_POSITION_PER_MARKET, MAX_LIABILITY_PER_MARKET_USDC)
  - Adjusted order size calculation respecting limits
- `rebates/active_quoting/momentum_detector.py` - Adverse selection protection with:
  - Track last_trade_price movements (via OrderbookManager on_trade callback)
  - Detect price moves >= MOMENTUM_THRESHOLD_TICKS within MOMENTUM_WINDOW_MS
  - Detect book sweeps (sudden depth removal >= SWEEP_DEPTH_THRESHOLD)
  - Trigger cooldown periods (COOLDOWN_SECONDS)
  - Cooldown expiry logic
- `rebates/active_quoting/quote_engine.py` - Updated with InventoryManager integration:
  - Optional InventoryManager in constructor
  - `calculate_quote_with_manager()` for automatic inventory lookup
  - `calculate_quote_for_side_with_manager()` with position limit checks
  - `get_inventory_adjusted_sizes()` for limit-aware order sizing
- Unit tests: 44 inventory_manager, 33 momentum_detector, 11 quote_engine integration (total: 88)
- Integration tests: 10 tests in test_inventory_tracking.py
  - Inventory updates from simulated fills ✓
  - User channel fill updates inventory ✓
  - Momentum detection with live trades ✓
  - Sweep detection with orderbook updates ✓
  - Quote skew changes with inventory ✓
  - Live orderbook quote with inventory ✓
  - Position limits block orders ✓
  - Adjusted order sizes ✓
  - Momentum cooldown blocks quotes ✓
  - Full quote cycle with inventory and momentum ✓

### Phase 4 Completed Components
- `rebates/active_quoting/risk_manager.py` - RiskManager with:
  - Per-market drawdown tracking (realized + unrealized P&L)
  - Global drawdown tracking across all markets
  - Stale feed detection (no orderbook update within threshold)
  - Circuit breaker states: NORMAL, WARNING, HALTED, RECOVERING
  - State transition logic with configurable thresholds
  - Recovery logic (gradual re-entry after halt)
  - Position limit multipliers based on state (100%/50%/25%/0%)
  - WebSocket disconnect handling (market WS -> WARNING, user WS -> HALT)
  - Kill switch integration with OrderManager via callbacks
  - Error tracking with consecutive error threshold
- Config updates:
  - Added `circuit_breaker_recovery_seconds` parameter (default: 60s)
- Unit tests: 66 tests in test_risk_manager.py
  - MarketRiskState and GlobalRiskState data classes
  - P&L tracking and drawdown calculation
  - Stale feed detection
  - Circuit breaker state transitions
  - Position limit multipliers
  - Error tracking
  - Disconnect handling
  - Kill switch integration
- Integration tests: 16 tests in test_risk_management.py
  - Per-market drawdown halts market ✓
  - Global drawdown triggers full halt ✓
  - Orders cancelled on halt ✓
  - Position limits reduced on WARNING (50%) ✓
  - Position limits reduced on RECOVERING (25%) ✓
  - Market WS disconnect triggers WARNING ✓
  - User WS disconnect triggers HALT ✓
  - Integrated WS disconnect flow ✓
  - Recovery process from HALT to NORMAL ✓
  - Cannot recover until halted ✓
  - Stale feed triggers WARNING ✓
  - Fresh feed clears stale status ✓
  - Inventory limits respect circuit breaker ✓
  - P&L tracking from fills ✓
  - One market halt doesn't affect others ✓
  - Global halt affects all markets ✓

### Phase 5 Completed Components
- `rebates/active_quoting/fill_analytics.py` - FillAnalytics with:
  - Per-fill tracking with timestamps, prices, and fees
  - Markout calculation at multiple horizons (1s, 5s, 15s, 30s, 60s)
  - Toxicity scoring (adverse selection measurement)
  - Per-market statistics (fills, volume, P&L)
  - Aggregate statistics across all markets
  - Fee tracking (maker rebates earned vs fees paid)
- `rebates/active_quoting/bot.py` - ActiveQuotingBot orchestration with:
  - Component wiring (OrderbookManager, UserChannelManager, QuoteEngine, OrderManager, etc.)
  - Market discovery from MarketFinder
  - Startup sequence (connect WebSockets → fetch initial state → start quoting)
  - Main loop (process orderbook updates → calculate quotes → place/update orders)
  - Graceful shutdown with order cancellation
  - Event callbacks for fills, book updates, trades, momentum, kill switch
  - Rate limiting (per-market and global refresh caps)
  - Status reporting with per-market and global statistics
  - Multi-market support with shared risk limits
  - Dry-run mode for safe testing
  - CLI entry point with argparse
- Unit tests: 50 fill_analytics, 34 bot (total: 84)
- Integration tests: 18 tests in test_full_bot.py
  - WebSocket connectivity (mocked) ✓
  - Quote cycles with simulated orderbooks ✓
  - Reconnect handling ✓
  - Markout tracking and capture ✓
  - Multi-market coordination ✓
  - Graceful shutdown with order cancellation ✓
  - Status monitoring ✓
  - Fill event processing ✓
  - Circuit breaker integration ✓

## Overview

This document outlines the implementation plan for upgrading the 15-minute crypto rebates bot from the passive "Only 50" strategy to an active two-sided quoting strategy optimized for rebate farming.

## Build Approach: New Module, Reuse Infrastructure

**Decision**: Build active quoting as a new module (`active_quoting/`) rather than refactoring `rebates_bot.py`.

**Rationale**:
1. Fundamentally different architecture (real-time WebSocket vs REST polling)
2. Different state machine (active quoting vs passive fire-and-forget)
3. Old "Only 50" strategy still works - can run both and A/B test
4. Easier to test without breaking existing functionality
5. Cleaner code - no awkward conditionals mixing two paradigms

**Reusable Components**:
| Component | Action | Notes |
|-----------|--------|-------|
| `market_finder.py` | Reuse as-is | Market discovery works fine |
| `PolymarketClient` | Extend | Add batch orders, fee handling |
| Telegram alerts | Extend | Add new alert types |
| Supabase patterns | Adapt | New tables for active quoting |
| Config patterns | Reuse | Same env var approach |

**Final Structure**:
```
rebates/
├── rebates_bot.py          # KEEP - "Only 50" still works
├── market_finder.py        # KEEP - shared between strategies
├── strategy.py             # KEEP - for Only 50
├── config.py               # KEEP - Only 50 config
├── active_quoting/         # NEW - all new code here
│   ├── __init__.py
│   ├── bot.py              # Main orchestration
│   ├── config.py           # Active quoting config
│   ├── models.py           # Data classes
│   ├── user_channel_manager.py
│   ├── orderbook_manager.py
│   ├── quote_engine.py
│   ├── momentum_detector.py
│   ├── inventory_manager.py
│   ├── risk_manager.py
│   ├── order_manager.py
│   ├── fill_analytics.py
│   └── run.py              # CLI entry point
```

## Current vs New Strategy Comparison

### Current: "Only 50" Strategy
- Places fixed BUY orders at exactly $0.50 on both UP and DOWN tokens
- Orders sit passively until filled
- No dynamic price adjustments
- No momentum detection
- Simple inventory tracking via imbalance check

### New: Active Two-Sided Quoting
- Dynamic quoting at best bid/ask (offset 0, improve only when spread is wide)
- Quote refresh with hysteresis (only when price moves ≥1-2 ticks from target)
- Momentum detection with automatic pullback
- Aggressive inventory skewing to stay delta-neutral
- Post-only enforcement to guarantee maker status
- **Authoritative order/fill state from user WebSocket channel**
- Fill toxicity tracking (markout analysis)
- Hard risk limits with kill switches
- **Batch order placement (up to 15 orders per request)**

## Key Components to Implement

### 1. OrderbookManager
Real-time orderbook tracking via WebSocket market channel:
- Best bid/ask extraction (via `best_bid_ask` events for low-latency)
- Full book snapshots via `book` events
- Incremental updates via `price_change` events
- **Handle `tick_size_change` events** (tick size can change mid-session)
- Spread calculation
- Book depth analysis
- Last trade price tracking (`last_trade_price` events)

### 2. UserChannelManager (NEW - Critical)
**Authoritative order and fill state** via authenticated user WebSocket:
- URI: `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- Subscribes to order fills, cancellations, order status updates
- Reconciles internal open-order map with exchange state
- **Prevents desync** (thinking you're quoting when you're not, or vice versa)
- Updates InventoryManager on fills

Without this component, the bot will eventually get desynced and either:
- Think orders are open when they've been filled/cancelled
- Think it has no orders when it actually does

### 3. QuoteEngine
Dynamic quote pricing with:
- **Quote at best_bid / best_ask (offset 0 by default)**
- Improve by 1 tick only when spread is wide enough
- Inventory skew based on position imbalance
- Spread widening during momentum
- Clamping to prevent crossing spread
- Post-only enforcement (let API reject if would cross)

### 4. MomentumDetector
Adverse selection protection:
- Track last trade price movements (via `last_trade_price` events)
- Detect book sweeps (sudden depth removal)
- Detect imbalance (one side thins out)
- Trigger cooldown periods (1-5 seconds)

### 5. InventoryManager
Position tracking and skewing:
- Track inventory per market (authoritative from UserChannelManager fills)
- Calculate skew factor based on position
- Apply linear/exponential skew to quotes
- Enforce hard position limits
- **Track worst-case liability**, not just share count

### 6. RiskManager
Circuit breaker and exposure limits:
- Max position per market (worst-case liability in USDC)
- Max total exposure across all markets
- **Per-market drawdown limit** (stop quoting that market)
- **Global drawdown limit** (kill switch)
- Disconnect detection (user channel disconnect = cancel all)
- **Stale feed = (no WS events AND cannot confirm order state)**

### 7. OrderManager
Order placement and cancellation with:
- **feeRateBps handling** (required for fee-enabled 15-min markets)
- Call `GET /fee-rate?token_id=...` before order creation
- Include `feeRateBps` in signed orders
- Post-only flag enforcement
- **Batch order placement** (up to 15 orders per request)
- Smart cancel/replace with hysteresis

### 8. FillAnalytics (Toxicity Tracking)
Measure strategy effectiveness:
- Markout at +1s, +5s, +15s after fill
- Round-trip completion rate
- Rebate per $ executed
- P&L attribution (rebates vs spread vs adverse selection)

## Implementation Phases (Test-Driven)

**Principle**: Full unit test coverage at every step. Integration tests must pass before moving to next phase. No code without tests.

### Phase 1: Foundation + WebSocket Connectivity
**Goal**: Prove we can connect to both WebSocket channels and receive events.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 1.1 | `config.py` + `models.py` | Data class validation | - |
| 1.2 | `orderbook_manager.py` | All event types, tick size changes | Connect to market WS, receive book updates for 1 market |
| 1.3 | `user_channel_manager.py` | Order state reconciliation, fill handling | Connect to user WS, verify auth, receive order events |

**Integration Gate**: Both WebSockets connect, stay connected for 5 min, receive expected events.

### Phase 2: Quote Engine + Order Management
**Goal**: Calculate quotes and place/cancel orders correctly.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 2.1 | `quote_engine.py` | Offset 0, improvement, skewing, hysteresis | - |
| 2.2 | `order_manager.py` | Batch placement, fee handling, post-only | Place 1 order, verify on user channel, cancel it |
| 2.3 | Basic quote cycle | End-to-end mock | Place quotes on 1 market, verify fills via user channel |

**Integration Gate**: Can place post-only orders, receive fill confirmations, cancel orders. Fee handling works.

### Phase 3: Inventory + Momentum Protection
**Goal**: Track positions and protect against adverse selection.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 3.1 | `inventory_manager.py` | Position updates, liability calc, skewing | Verify inventory updates from user channel fills |
| 3.2 | `momentum_detector.py` | Trade detection, sweep detection, cooldown | Detect real momentum on live market |
| 3.3 | Quote skewing | Skew adjusts based on inventory | Fill one side, verify quote skew changes |

**Integration Gate**: Inventory tracks correctly. Momentum detection triggers on real price moves.

### Phase 4: Risk Management + Circuit Breaker
**Goal**: Hard limits and kill switches work correctly.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 4.1 | `risk_manager.py` | Per-market/global drawdown, stale feed | - |
| 4.2 | Circuit breaker integration | State transitions, recovery | Simulate limit breach, verify orders cancelled |
| 4.3 | Disconnect handling | Kill switch on WS drop | Kill WS connection, verify all orders cancelled |

**Integration Gate**: Position limits enforced. Disconnect triggers order cancellation.

### Phase 5: Full Bot + Analytics
**Goal**: Complete system running on multiple markets.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 5.1 | `fill_analytics.py` | Markout calculation, P&L tracking | - |
| 5.2 | `bot.py` orchestration | Full quote cycle with mocks | Run on 1 market for 15 min |
| 5.3 | Multi-market | Batch ordering across markets | Run on 3 markets (BTC, ETH, SOL) for 1 hour |

**Integration Gate**: Bot runs stable on 3 markets. No desync. Markout tracking works.

### Phase 6: Production Hardening
**Goal**: Ready for full deployment.

| Step | Component | Unit Tests | Integration Test |
|------|-----------|------------|------------------|
| 6.1 | Telegram alerts | Alert formatting | Alerts fire on fills, errors, limits |
| 6.2 | Database persistence | - | State survives restart |
| 6.3 | Full deployment | - | Run on 16 markets for 24 hours |

**Integration Gate**: 24-hour run with no crashes, correct state, alerts working.

## Technical Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       ActiveQuotingBot                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │   Market     │───▶│  Orderbook   │───▶│   Momentum   │          │
│  │  WebSocket   │    │  Manager     │    │   Detector   │          │
│  └──────────────┘    └──────────────┘    └──────────────┘          │
│         │                   │                   │                   │
│         │                   ▼                   ▼                   │
│         │           ┌──────────────┐    ┌──────────────┐           │
│         │           │   Quote      │◀───│  Inventory   │           │
│         │           │   Engine     │    │  Manager     │           │
│         │           └──────────────┘    └──────────────┘           │
│         │                   │                   ▲                   │
│         │                   ▼                   │                   │
│         │           ┌──────────────┐    ┌──────────────┐           │
│         │           │   Order      │───▶│    Risk      │           │
│         │           │   Manager    │    │   Manager    │           │
│         │           └──────────────┘    └──────────────┘           │
│         │                   │                   │                   │
│         │                   ▼                   │                   │
│  ┌──────────────┐   ┌──────────────┐           │                   │
│  │    User      │──▶│    Fill      │───────────┘                   │
│  │  WebSocket   │   │  Analytics   │                               │
│  └──────────────┘   └──────────────┘                               │
│         │                                                           │
│         └──────────▶ InventoryManager (authoritative fills)        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Parameter Defaults (Starting Point)

### Quote Pricing
- `QUOTE_OFFSET_TICKS`: 0 (quote AT best bid/ask, not inside)
- `IMPROVE_WHEN_SPREAD_TICKS`: 4 (only improve by 1 tick if spread ≥ 4 ticks)
- `MAX_SPREAD_TICKS`: 10 (widen to this during momentum)

**Rationale**: You earn rebates quoting at best bid/ask too. Post-only ensures you don't take. Only improve when there's room.

### Quote Refresh (Hysteresis)
- `REFRESH_THRESHOLD_TICKS`: 2 (only refresh if quote is ≥2 ticks from target)
- `MIN_REFRESH_INTERVAL_MS`: 500 (per market, prevents churn)
- `GLOBAL_REFRESH_CAP_PER_SEC`: 10 (across all 16 markets)

**Rationale**: Constant cancel/replace loses queue priority and risks rate limits. Refresh only when meaningfully off-target.

### Momentum Detection
- `MOMENTUM_THRESHOLD_TICKS`: 3 (trigger if price moves 3+ ticks)
- `MOMENTUM_WINDOW_MS`: 500 (within 500ms)
- `COOLDOWN_SECONDS`: 2 (pause after momentum)
- `SWEEP_DEPTH_THRESHOLD`: 0.5 (50% depth removed = sweep)

### Inventory Management
- `MAX_POSITION_PER_MARKET`: 100 (shares)
- `MAX_LIABILITY_PER_MARKET_USDC`: 50 (worst-case loss = shares × entry_price)
- `MAX_TOTAL_LIABILITY_USDC`: 500 (across all markets)
- `INVENTORY_SKEW_COEFFICIENT`: 0.1 (linear: skew = coef × inventory)

**Exposure calculation**: For binaries, worst-case loss per share ≈ entry price (if goes to 0). So 100 shares @ $0.50 = $50 max liability.

### Risk Management
- `MAX_DRAWDOWN_PER_MARKET_USDC`: 20 (stop quoting that market)
- `MAX_DRAWDOWN_GLOBAL_USDC`: 100 (kill switch)
- `STALE_FEED_LOGIC`: "no WS events AND cannot confirm order state" (not just timer)
- `MAX_CONSECUTIVE_ERRORS`: 5

### Order Management
- `ORDER_SIZE`: 10 (USDC per side)
- `BATCH_SIZE`: 15 (max orders per batch request)
- `CANCEL_ON_MOMENTUM`: True
- `POST_ONLY`: True (always)

### Fee Handling
- `FEE_CACHE_TTL_SECONDS`: 300 (cache fee rates for 5 min)
- Always fetch `feeRateBps` via `GET /fee-rate?token_id=...` before orders

## File Structure

```
rebates/
├── active_quoting/
│   ├── __init__.py
│   ├── bot.py                   # Main ActiveQuotingBot class
│   ├── orderbook_manager.py     # Real-time orderbook tracking
│   ├── user_channel_manager.py  # Authoritative order/fill state (NEW)
│   ├── quote_engine.py          # Dynamic quote pricing
│   ├── momentum_detector.py     # Adverse selection detection
│   ├── inventory_manager.py     # Position tracking and skewing
│   ├── risk_manager.py          # Circuit breaker and limits
│   ├── fill_analytics.py        # Toxicity/markout tracking
│   ├── order_manager.py         # Batch order placement with fees
│   ├── config.py                # Configuration parameters
│   └── models.py                # Data classes
├── tests/
│   ├── test_orderbook_manager.py
│   ├── test_user_channel_manager.py
│   ├── test_quote_engine.py
│   ├── test_momentum_detector.py
│   ├── test_inventory_manager.py
│   ├── test_risk_manager.py
│   ├── test_fill_analytics.py
│   └── test_order_manager.py
```

## WebSocket Integration

### Market Channel (Public)
- URI: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe: `{"assets_ids": [token1, token2, ...]}`
- **Events to handle**:
  - `book` - Full orderbook snapshot
  - `price_change` - Incremental book updates
  - `best_bid_ask` - Low-latency top-of-book (use this for quote decisions)
  - `last_trade_price` - For momentum detection
  - `tick_size_change` - **Critical**: tick size can change mid-session

### User Channel (Authenticated) - CRITICAL
- URI: `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- Auth: API credentials (apiKey, secret, passphrase)
- **Events to handle**:
  - Order fills
  - Order cancellations
  - Order status updates

**Why this is critical**: The public market channel does NOT tell you about your orders. Without the user channel, you will eventually get desynced.

## Quoting Logic (Pseudocode)

```python
def calculate_quotes(orderbook, inventory, momentum_state, current_orders):
    if momentum_state.is_active:
        return CancelAll()  # Don't quote during momentum

    best_bid = orderbook.best_bid
    best_ask = orderbook.best_ask
    spread = best_ask - best_bid
    tick = orderbook.tick_size

    # Default: quote AT best bid/ask
    my_bid = best_bid
    my_ask = best_ask

    # Only improve if spread is wide enough
    if spread >= IMPROVE_WHEN_SPREAD_TICKS * tick:
        my_bid = best_bid + tick
        my_ask = best_ask - tick

    # Clamp to avoid crossing (post-only will reject anyway, but be explicit)
    if my_bid >= best_ask:
        my_bid = best_bid
    if my_ask <= best_bid:
        my_ask = best_ask

    # Apply inventory skew (positive inventory = long, want to sell)
    skew_ticks = int(INVENTORY_SKEW_COEFFICIENT * inventory)
    my_bid -= skew_ticks * tick  # Less aggressive buying if long
    my_ask -= skew_ticks * tick  # More aggressive selling if long

    # Final clamp
    my_bid = max(my_bid, tick)  # Don't go below min price
    my_ask = min(my_ask, 1.0 - tick)  # Don't go above max price
    my_bid = min(my_bid, best_ask - tick)
    my_ask = max(my_ask, best_bid + tick)

    # Check if refresh is needed (hysteresis)
    if current_orders:
        bid_diff = abs(current_orders.bid_price - my_bid)
        ask_diff = abs(current_orders.ask_price - my_ask)
        if bid_diff < REFRESH_THRESHOLD_TICKS * tick and ask_diff < REFRESH_THRESHOLD_TICKS * tick:
            return KeepCurrentOrders()  # Not enough change to refresh

    return Quote(bid=my_bid, ask=my_ask, size=ORDER_SIZE)
```

## Batch Order Flow

```python
async def refresh_all_quotes():
    """Refresh quotes across all markets in batches of 15."""
    orders_to_place = []

    for market in active_markets:
        quote = calculate_quotes(
            orderbook=orderbook_manager.get(market),
            inventory=inventory_manager.get(market),
            momentum_state=momentum_detector.get(market),
            current_orders=order_manager.get_open(market)
        )

        if quote.should_cancel:
            # Cancel immediately (don't batch cancels)
            await order_manager.cancel_market(market)
        elif quote.should_update:
            orders_to_place.append(quote.to_order(market))

    # Batch place orders (max 15 per request)
    for batch in chunks(orders_to_place, 15):
        await order_manager.place_batch(batch)
```

## Fee Handling

```python
class FeeManager:
    """Manages feeRateBps for 15-minute crypto markets."""

    def __init__(self, client):
        self.client = client
        self.cache = {}  # token_id -> (feeRateBps, timestamp)

    async def get_fee_rate(self, token_id: str) -> int:
        """Get fee rate, with caching."""
        if token_id in self.cache:
            fee, ts = self.cache[token_id]
            if time.time() - ts < FEE_CACHE_TTL_SECONDS:
                return fee

        # Fetch from API
        resp = await self.client.get(f"/fee-rate?token_id={token_id}")
        fee = resp.get("feeRateBps", 0)
        self.cache[token_id] = (fee, time.time())
        return fee

    def sign_order_with_fee(self, order: dict, fee_rate_bps: int) -> dict:
        """Include feeRateBps in signed order."""
        order["feeRateBps"] = fee_rate_bps
        return self.client.sign_order(order)
```

## Testing Strategy

**Principle**: No code without tests. Integration tests gate each phase.

### Test Infrastructure
```
tests/
├── unit/
│   └── active_quoting/
│       ├── test_config.py
│       ├── test_models.py
│       ├── test_orderbook_manager.py
│       ├── test_user_channel_manager.py
│       ├── test_quote_engine.py
│       ├── test_momentum_detector.py
│       ├── test_inventory_manager.py
│       ├── test_risk_manager.py
│       ├── test_order_manager.py
│       └── test_fill_analytics.py
├── integration/
│   └── active_quoting/
│       ├── test_websocket_connectivity.py    # Phase 1 gate
│       ├── test_order_lifecycle.py           # Phase 2 gate
│       ├── test_inventory_tracking.py        # Phase 3 gate
│       ├── test_risk_limits.py               # Phase 4 gate
│       └── test_full_bot.py                  # Phase 5 gate
└── conftest.py                               # Shared fixtures
```

### Unit Test Coverage Requirements
Each component must have tests for:

1. **OrderbookManager**: All event types (book, price_change, best_bid_ask, tick_size_change), edge cases (empty book, crossed book)
2. **UserChannelManager**: Order state reconciliation, fill handling, desync detection, reconnection
3. **QuoteEngine**: Offset 0 default, improvement only when spread wide, inventory skewing, hysteresis logic
4. **MomentumDetector**: Trade detection, sweep detection, cooldown timing, cooldown expiry
5. **InventoryManager**: Position updates from fills, liability calculation, limit enforcement, skew factor
6. **RiskManager**: Per-market and global drawdowns, stale feed logic (WS + order state), error counting
7. **FillAnalytics**: Markout calculation at +1s/+5s/+15s, P&L tracking, toxicity scoring
8. **OrderManager**: Batch placement (≤15), fee handling, post-only enforcement, cancel logic

### Integration Test Gates

**Phase 1 Gate** (`test_websocket_connectivity.py`):
```python
@pytest.mark.integration
async def test_market_ws_connects_and_receives_book():
    """Market WebSocket connects and receives book events."""

@pytest.mark.integration
async def test_user_ws_connects_and_authenticates():
    """User WebSocket connects with credentials."""

@pytest.mark.integration
async def test_both_ws_stable_for_5_minutes():
    """Both WebSockets stay connected for 5 minutes."""
```

**Phase 2 Gate** (`test_order_lifecycle.py`):
```python
@pytest.mark.integration
async def test_place_post_only_order():
    """Place a post-only order and verify on user channel."""

@pytest.mark.integration
async def test_cancel_order():
    """Cancel order and verify cancellation on user channel."""

@pytest.mark.integration
async def test_fee_rate_fetching():
    """Fetch feeRateBps for 15-min market token."""
```

**Phase 3 Gate** (`test_inventory_tracking.py`):
```python
@pytest.mark.integration
async def test_fill_updates_inventory():
    """Order fill updates inventory via user channel."""

@pytest.mark.integration
async def test_momentum_detection_live():
    """Momentum detector triggers on real price moves."""
```

**Phase 4 Gate** (`test_risk_limits.py`):
```python
@pytest.mark.integration
async def test_position_limit_stops_quoting():
    """Hitting position limit stops quoting that market."""

@pytest.mark.integration
async def test_disconnect_cancels_all_orders():
    """WebSocket disconnect triggers order cancellation."""
```

**Phase 5 Gate** (`test_full_bot.py`):
```python
@pytest.mark.integration
@pytest.mark.slow
async def test_single_market_15_minutes():
    """Bot runs on 1 market for 15 minutes without errors."""

@pytest.mark.integration
@pytest.mark.slow
async def test_three_markets_1_hour():
    """Bot runs on 3 markets for 1 hour without errors."""
```

### Running Tests
```bash
# Run all unit tests for active_quoting
uv run pytest tests/unit/active_quoting/ -v

# Run Phase 1 integration tests (requires .env credentials)
POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/test_websocket_connectivity.py -v

# Run all integration tests
POLY_TEST_INTEGRATION=true uv run pytest tests/integration/active_quoting/ -v

# Run with coverage
uv run pytest tests/unit/active_quoting/ --cov=rebates.active_quoting --cov-report=term-missing
```

## Metrics to Track

### Real-time
- Current position per market (from user channel)
- Open orders per market (from user channel)
- Quote spread being offered
- Time in cooldown per market
- Current worst-case liability

### Daily aggregates
- Total maker volume executed
- Rebates earned (from Polymarket dashboard)
- Spread capture P&L
- Markout P&L (adverse selection cost)
- Net P&L = rebates + spread - markout

### Per-market
- Fill rate (orders filled / orders placed)
- Average markout at +1s, +5s, +15s
- Toxicity score (bad fills / total fills)
- Refresh rate (cancels per minute)

## Rollout Plan

1. **Test on 1 market**: Single BTC 15-min market with minimal size ($10)
2. **Validate user channel**: Confirm order state stays synced for 24h
3. **Expand to 3 markets**: BTC, ETH, SOL with same parameters
4. **Tune parameters**: Based on 24-72 hours of data
5. **Scale to 16 markets**: Full deployment with batch ordering
6. **Increase size**: Gradually increase order size based on fill rates and markout

## References

- [Market Channel Events](https://docs.polymarket.com/developers/CLOB/websocket/market-channel)
- [Data Feeds (incl. User Channel)](https://docs.polymarket.com/developers/market-makers/data-feeds)
- [Maker Rebates Program](https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program)
- [Fee Rate API](https://docs.polymarket.com/developers/market-makers/maker-rebates-program)
- [Batch Order Placement](https://docs.polymarket.com/developers/CLOB/orders/create-order-batch)
- [CLOB Introduction](https://docs.polymarket.com/developers/CLOB/introduction)
- [Orders Overview](https://docs.polymarket.com/developers/CLOB/orders/orders)
- [WSS Overview](https://docs.polymarket.com/developers/CLOB/websocket/wss-overview)
