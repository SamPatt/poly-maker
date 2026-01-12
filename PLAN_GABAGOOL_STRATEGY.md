# Implementation Plan: Gabagool Arbitrage Strategy

## Executive Summary

This plan details how to implement the "Gabagool" paired position arbitrage strategy in the existing rebates bot. The strategy guarantees profit by purchasing both YES and NO positions when their combined cost falls below $1.00 (minus fees).

**Key Insight**: Unlike the current rebates strategy that targets maker rebates, Gabagool targets **mathematical arbitrage** - profit that's guaranteed regardless of market outcome.

---

## Table of Contents

1. [Strategy Overview](#1-strategy-overview)
2. [Profitability Analysis](#2-profitability-analysis)
3. [Architecture Design](#3-architecture-design)
4. [Opportunity Detection](#4-opportunity-detection)
5. [Order Execution](#5-order-execution)
6. [Position Management](#6-position-management)
7. [Profit Realization](#7-profit-realization)
8. [Risk Management](#8-risk-management)
9. [Configuration Parameters](#9-configuration-parameters)
10. [Implementation Phases](#10-implementation-phases)
11. [Testing Strategy](#11-testing-strategy)
12. [Monitoring & Alerting](#12-monitoring--alerting)

---

## 1. Strategy Overview

### What is the Gabagool Strategy?

The Gabagool strategy exploits a mathematical invariant in binary prediction markets:

```
YES + NO = $1.00 at settlement
```

When the combined market price of YES and NO falls below $1.00, buying both guarantees profit:

```
Profit = $1.00 - (YES_price + NO_price) - fees
```

### How It Differs from Current Rebates Strategy

| Aspect | Current Rebates Strategy | Gabagool Strategy |
|--------|-------------------------|-------------------|
| Profit source | Maker rebates (~1.56% at 50%) | Mathematical arbitrage |
| Target price | Near 50% for max rebates | Any price where YES+NO < threshold |
| Risk profile | Delta-neutral (both sides filled) | Risk-free (guaranteed profit) |
| Fill requirement | Both sides should fill | **Both sides MUST fill** |
| Opportunity frequency | Every market | Only when mispricing exists |
| Profit per trade | ~$0.08 per $10 traded | Variable (typically 2-5% of position) |

### Real-World Validation

- **Gabagool trader**: Documented consistent profits using this exact strategy
- **Bot success**: $313 → $437,600 in one month with 98% win rate
- **Academic research**: $40M extracted via arbitrage strategies (Apr 2024-Apr 2025)

---

## 2. Profitability Analysis

### Fee Structure for 15-Minute Markets

```
Polymarket 15-min crypto market fees:
- Maker: 0% (FREE - and you EARN rebates!)
- Taker: Up to 1.56% at 50% odds (dynamic, funds maker rebates)
- Winner fee: NONE (no fee on profits)
- Gas: ~$0.001 on Polygon (negligible)
```

**Key Insight**: There is NO 2% winner fee on Polymarket. The only fees are taker fees on 15-minute crypto markets, which fund maker rebates. This makes Gabagool significantly more profitable than previously calculated.

### Minimum Profitable Spread Calculation

```python
# Variables
combined_cost = yes_price + no_price  # e.g., 0.99
payout = 1.00                          # Guaranteed at settlement/merge
gas_cost = 0.002                       # ~$0.002 for merge tx on Polygon

# Profit calculation (MAKER orders - no taker fee)
gross_profit = payout - combined_cost
net_profit = gross_profit - gas_cost

# Break-even point (maker orders)
min_spread = gas_cost + safety_margin
# min_spread = 0.002 + 0.003 = 0.005 (0.5%)

# Therefore: Profitable when combined_cost < 0.995 (maker)
# Conservative threshold: combined_cost < 0.99 (1% margin)

# If using TAKER orders (pay up to 1.56% fee at 50% odds):
taker_fee = 0.0156  # Maximum at 50% price
min_spread_taker = taker_fee + gas_cost + safety_margin
# min_spread_taker = 0.0156 + 0.002 + 0.003 = 0.0206 (~2.1%)

# Therefore: Profitable when combined_cost < 0.98 (taker)
```

### Position Size Considerations

With no winner fee, spreads can be much tighter:

| Order Type | Minimum Spread Required | Reason |
|------------|------------------------|--------|
| Maker | 0.5-1.0% | Only gas costs |
| Taker | 2.0-2.5% | Taker fee (~1.56%) + gas |
| Hybrid | 1.0-1.5% | Start maker, rescue with taker |

**Recommendation**: Trade size of $50-100 per opportunity:
- Maker orders: Profitable at 1%+ spread (~$0.50-1.00 profit)
- Taker rescue: Still profitable at 2%+ spread
- Low execution risk with meaningful profit

### Expected Returns

Assuming:
- Average spread when opportunity exists: 2% (more opportunities with lower threshold!)
- Opportunity frequency: 3-6 times per hour (more frequent with tighter threshold)
- Trade size: $100 per opportunity
- Maker execution (no taker fee)

```
Per opportunity: $100 × 2% - $0.002 gas = $1.998 profit
Per hour (4 opps): $8.00
Per day (24 hrs): $192.00
Per month: ~$5,700 (theoretical maximum)
```

**Conservative estimate** (accounting for partial fills, missed opportunities):
- 50% execution rate: ~$2,800/month
- 30% execution rate: ~$1,700/month

**Note**: Actual returns depend on opportunity frequency and execution success rate.

---

## 3. Architecture Design

### Integration with Existing Rebates Bot

```
┌─────────────────────────────────────────────────────────────┐
│                     RebatesBot (main.py)                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────┐    ┌─────────────────────────────┐     │
│  │ Market Finder   │    │  Opportunity Scanner (NEW)  │     │
│  │ (existing)      │    │  - Monitors orderbooks      │     │
│  │                 │    │  - Detects YES+NO < 0.99    │     │
│  └────────┬────────┘    └──────────────┬──────────────┘     │
│           │                            │                    │
│           ▼                            ▼                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Strategy Selector (NEW)                │    │
│  │  - Rebates strategy (existing)                      │    │
│  │  - Gabagool strategy (NEW)                          │    │
│  │  - Selects best strategy per market/opportunity     │    │
│  └─────────────────────────────────────────────────────┘    │
│           │                            │                    │
│           ▼                            ▼                    │
│  ┌─────────────────┐    ┌─────────────────────────────┐     │
│  │ Delta Neutral   │    │  Gabagool Executor (NEW)    │     │
│  │ Strategy        │    │  - Simultaneous orders      │     │
│  │ (existing)      │    │  - Position tracking        │     │
│  │                 │    │  - Merge execution          │     │
│  └─────────────────┘    └─────────────────────────────┘     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### New Module Structure

```
rebates/
├── rebates_bot.py          # Main bot (modify to add Gabagool mode)
├── market_finder.py        # Market discovery (no changes)
├── strategy.py             # Existing delta-neutral strategy
├── gabagool/               # NEW: Gabagool strategy module
│   ├── __init__.py
│   ├── scanner.py          # Orderbook scanning for opportunities
│   ├── executor.py         # Order execution with fill guarantees
│   ├── position_tracker.py # Track paired positions
│   ├── circuit_breaker.py  # NEW: Safety system (from Rust reference)
│   ├── reconciler.py       # NEW: Auto-close mismatched fills (from Rust reference)
│   └── config.py           # Strategy-specific configuration
└── config.py               # Add Gabagool config options
```

### Components from Rust Reference

The following components are ported from the production Rust arbitrage bot in `/sampatt/cledo/reference/`:

| Python Module | Rust Source | Purpose |
|---------------|-------------|---------|
| `circuit_breaker.py` | `src/circuit_breaker.rs` | Position/loss limits, auto-halt |
| `reconciler.py` | `src/execution.rs` (auto_close_mismatch) | Fix mismatched fills |

These patterns are battle-tested in production cross-platform arbitrage and provide essential safety guarantees.

---

## 4. Opportunity Detection

### Scanning Algorithm

```python
class GabagoolScanner:
    """
    Continuously monitors orderbooks for arbitrage opportunities.

    An opportunity exists when:
    - YES ask + NO ask < PROFIT_THRESHOLD (e.g., 0.99 for maker, 0.98 for taker)
    - Sufficient liquidity exists at those prices
    - Market has enough time remaining

    Fee structure (15-min crypto markets):
    - Maker orders: FREE (no fees, plus earn rebates!)
    - Taker orders: Up to 1.56% at 50% odds
    - No winner fee on profits
    """

    def __init__(self, profit_threshold: float = 0.99):
        self.profit_threshold = profit_threshold  # 1% spread for maker orders
        self.min_liquidity = 50  # Minimum shares available

    def scan_market(self, up_token: str, down_token: str) -> Optional[Opportunity]:
        """
        Scan a single market for arbitrage opportunity.

        Returns Opportunity if found, None otherwise.
        """
        # Get orderbooks for both tokens
        up_book = self.get_orderbook(up_token)
        down_book = self.get_orderbook(down_token)

        # Get best ask prices (what we'd pay to buy)
        up_best_ask = self.get_best_ask(up_book)
        down_best_ask = self.get_best_ask(down_book)

        if up_best_ask is None or down_best_ask is None:
            return None

        combined_cost = up_best_ask.price + down_best_ask.price

        # Check profitability threshold
        if combined_cost >= self.profit_threshold:
            return None

        # Check liquidity
        max_size = min(up_best_ask.size, down_best_ask.size)
        if max_size < self.min_liquidity:
            return None

        # Calculate profit (NO winner fee - only gas costs!)
        gross_profit = 1.00 - combined_cost
        gas_cost = 0.002  # ~$0.002 for merge transaction
        net_profit = gross_profit - gas_cost  # Much better than before!

        return Opportunity(
            up_token=up_token,
            down_token=down_token,
            up_price=up_best_ask.price,
            down_price=down_best_ask.price,
            combined_cost=combined_cost,
            max_size=max_size,
            gross_profit_pct=gross_profit * 100,
            net_profit_pct=net_profit * 100
        )
```

### VWAP-Based Pricing (Anti-Spoofing Protection)

The existing `strategy.py` already implements VWAP protection. We should reuse this:

```python
def get_executable_price(
    self,
    orders: list,
    target_size: float,
    min_order_size: float = 5.0
) -> Tuple[Optional[float], float]:
    """
    Calculate the actual price we'd pay to fill target_size.

    Uses VWAP across multiple levels to protect against:
    - Spoofing (small fake orders)
    - Thin liquidity (single level exhaustion)

    Returns:
        Tuple of (weighted_avg_price, fillable_size)
    """
    if not orders:
        return None, 0

    # Sort asks by price (lowest first)
    sorted_orders = sorted(orders, key=lambda x: float(x["price"]))

    filled_size = 0
    total_cost = 0

    for order in sorted_orders:
        price = float(order["price"])
        size = float(order["size"])
        dollar_value = price * size

        # Skip small orders that might be spoofing
        if dollar_value < min_order_size:
            continue

        # Calculate how much we can fill from this level
        remaining = target_size - filled_size
        fill_from_level = min(size, remaining)

        filled_size += fill_from_level
        total_cost += fill_from_level * price

        if filled_size >= target_size:
            break

    if filled_size == 0:
        return None, 0

    avg_price = total_cost / filled_size
    return avg_price, filled_size
```

### Continuous Monitoring Loop

```python
async def monitor_opportunities(self):
    """
    Continuously monitor all active 15-minute markets for opportunities.

    Runs on a tight loop (1-2 second intervals) since opportunities
    can appear and disappear quickly.
    """
    while True:
        try:
            # Get all currently tradeable markets
            markets = self.finder.get_upcoming_markets()

            for market in markets:
                # Check for Gabagool opportunity
                opportunity = self.scanner.scan_market(
                    market["up_token"],
                    market["down_token"]
                )

                if opportunity and opportunity.net_profit_pct > 0.5:
                    # Found profitable opportunity!
                    await self.execute_gabagool(market, opportunity)

            # Short sleep to avoid rate limiting
            await asyncio.sleep(1)

        except Exception as e:
            self.log(f"Error in opportunity monitor: {e}")
            await asyncio.sleep(5)
```

---

## 5. Order Execution

### Critical: Simultaneous Execution

The biggest risk in Gabagool is **partial fill** - getting only one side filled and being exposed to directional risk.

**Best Practices from Research:**

1. **Use FOK (Fill-or-Kill) orders** when crossing the spread
2. **Use post-only maker orders** when liquidity allows
3. **Execute both legs in parallel** using async
4. **Cancel immediately** if one leg fails
5. **Track in-flight orders** to prevent double execution

### Execution Patterns

#### Pattern A: Aggressive Taker (Fast, Higher Cost)

```python
async def execute_taker(
    self,
    up_token: str,
    down_token: str,
    size: float,
    opportunity: Opportunity
) -> ExecutionResult:
    """
    Execute using taker orders for guaranteed immediate fill.

    Pros: Guaranteed fill, no timing risk
    Cons: Pays taker fee (up to 1.56%), reduces profit margin

    Only use when:
    - Spread is large enough to absorb taker fee
    - Time pressure (market about to go live)
    - High confidence opportunity will disappear
    """
    # Place both orders simultaneously
    up_task = self.place_taker_order(up_token, size, opportunity.up_price)
    down_task = self.place_taker_order(down_token, size, opportunity.down_price)

    # Wait for both with timeout
    try:
        up_result, down_result = await asyncio.wait_for(
            asyncio.gather(up_task, down_task),
            timeout=5.0
        )
    except asyncio.TimeoutError:
        # Cancel any pending orders
        await self.cancel_all(up_token, down_token)
        return ExecutionResult(success=False, reason="Timeout")

    # Verify both filled
    if not (up_result.success and down_result.success):
        # Handle partial fill scenario
        return await self.handle_partial_fill(up_result, down_result)

    return ExecutionResult(
        success=True,
        up_filled=up_result.size,
        down_filled=down_result.size,
        total_cost=up_result.cost + down_result.cost
    )
```

#### Pattern B: Maker-First (Slower, Lower Cost)

```python
async def execute_maker(
    self,
    up_token: str,
    down_token: str,
    size: float,
    opportunity: Opportunity
) -> ExecutionResult:
    """
    Execute using maker orders for zero taker fee.

    Pros: No taker fee, higher net profit
    Cons: May not fill, requires patience

    Use when:
    - Time available before market goes live
    - Spread is thin (need to save every cent)
    - Can accept some fill risk
    """
    # Calculate maker prices (best bid + 1 tick)
    up_maker_price = self.get_best_maker_price(up_token)
    down_maker_price = self.get_best_maker_price(down_token)

    # Verify combined maker price still profitable
    if up_maker_price + down_maker_price >= self.profit_threshold:
        return ExecutionResult(success=False, reason="Maker prices not profitable")

    # Place maker orders
    up_order = await self.place_maker_order(up_token, size, up_maker_price)
    down_order = await self.place_maker_order(down_token, size, down_maker_price)

    # Monitor for fills with timeout
    filled_up, filled_down = await self.wait_for_fills(
        up_order,
        down_order,
        timeout=30.0
    )

    # Handle partial fills
    if filled_up != filled_down:
        return await self.rescue_imbalance(filled_up, filled_down, up_token, down_token)

    return ExecutionResult(success=True, ...)
```

#### Pattern C: Hybrid (Recommended)

```python
async def execute_hybrid(
    self,
    up_token: str,
    down_token: str,
    size: float,
    opportunity: Opportunity,
    time_remaining: float
) -> ExecutionResult:
    """
    Hybrid approach: Start with maker, escalate to taker if needed.

    This maximizes profit while ensuring fills.

    Timeline:
    1. Place maker orders on both sides
    2. Monitor for fills
    3. If one side fills and other doesn't within 10s, rescue
    4. If <30s to market live, switch to taker
    """
    # Phase 1: Place maker orders
    up_order = await self.place_maker_order(up_token, size, opportunity.up_price - 0.01)
    down_order = await self.place_maker_order(down_token, size, opportunity.down_price - 0.01)

    # Phase 2: Monitor with escalation
    start_time = time.time()

    while time.time() - start_time < min(time_remaining - 30, 60):
        up_filled, down_filled = await self.check_fills(up_order, down_order)

        # Both filled - success!
        if up_filled and down_filled:
            return ExecutionResult(success=True, ...)

        # One filled, other not - rescue immediately
        if up_filled != down_filled:
            elapsed = time.time() - start_time
            if elapsed > 10:  # Give 10s for natural fill
                return await self.rescue_with_taker(
                    up_token if not up_filled else None,
                    down_token if not down_filled else None,
                    size
                )

        await asyncio.sleep(1)

    # Phase 3: Time running out - go taker
    return await self.force_taker_completion(up_token, down_token, size)
```

### Fill-or-Kill Implementation Notes

**Important**: The py-clob-client has precision limitations for FOK orders:

```python
# From research: FOK orders have strict precision requirements
# - Sell orders: maker amount max 2 decimal places
# - Taker amount: max 4 decimal places
# - size × price must not exceed 2 decimal places

def prepare_fok_order(size: float, price: float) -> Tuple[float, float]:
    """
    Adjust size and price to meet FOK precision requirements.
    """
    # Round size to 2 decimal places
    adjusted_size = round(size, 2)

    # Ensure price × size has max 2 decimal places
    product = adjusted_size * price
    if len(str(product).split('.')[-1]) > 2:
        # Reduce size slightly to fix precision
        adjusted_size = math.floor(size * 100) / 100

    return adjusted_size, price
```

---

## 6. Position Management

### TrackedGabagoolPosition Data Structure

```python
@dataclass
class TrackedGabagoolPosition:
    """Track a Gabagool arbitrage position from entry to profit realization."""

    # Identification
    id: str                      # Unique position ID
    market_slug: str
    condition_id: str

    # Tokens
    up_token: str
    down_token: str
    neg_risk: bool

    # Entry details
    entry_time: datetime
    up_entry_price: float
    down_entry_price: float
    combined_cost: float
    position_size: float         # Shares held

    # Fill tracking
    up_filled: float = 0.0
    down_filled: float = 0.0
    is_balanced: bool = False    # True when up_filled == down_filled

    # State machine
    status: str = "PENDING"      # PENDING -> FILLED -> MERGED/RESOLVED

    # Profit tracking
    expected_profit: float = 0.0
    realized_profit: float = 0.0

    # Timestamps
    market_resolution_time: Optional[datetime] = None
    merge_time: Optional[datetime] = None

    @property
    def min_filled(self) -> float:
        """The minimum of up and down fills (mergeable amount)."""
        return min(self.up_filled, self.down_filled)

    @property
    def imbalance(self) -> float:
        """Difference between up and down fills (directional exposure)."""
        return abs(self.up_filled - self.down_filled)
```

### Position State Machine

```
┌─────────────────────────────────────────────────────────────────┐
│                    Position State Machine                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   PENDING ──────► PARTIALLY_FILLED ──────► FILLED               │
│      │                    │                    │                │
│      │                    │                    │                │
│      │                    ▼                    │                │
│      │               REBALANCING               │                │
│      │                    │                    │                │
│      │                    ▼                    │                │
│      └──────────────► FILLED ◄────────────────┘                │
│                          │                                      │
│                          │                                      │
│            ┌─────────────┴─────────────┐                        │
│            ▼                           ▼                        │
│      MERGE_READY                  AWAIT_RESOLUTION              │
│            │                           │                        │
│            ▼                           ▼                        │
│        MERGED                     RESOLVED                      │
│            │                           │                        │
│            └───────────► PROFIT_REALIZED ◄─────────────────────┘│
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

State Transitions:
- PENDING: Orders placed, awaiting fills
- PARTIALLY_FILLED: One side filled, other pending
- REBALANCING: Actively rescuing imbalanced position
- FILLED: Both sides filled (may or may not be balanced)
- MERGE_READY: Position balanced, ready to merge for immediate profit
- AWAIT_RESOLUTION: Position held until market resolution
- MERGED: Merge transaction completed
- RESOLVED: Market resolved, winning side redeemed
- PROFIT_REALIZED: Profit collected and accounted
```

### Database Schema

```sql
-- Add to existing rebates_markets table or create new table
CREATE TABLE gabagool_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Market identification
    market_slug TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    up_token TEXT NOT NULL,
    down_token TEXT NOT NULL,
    neg_risk BOOLEAN DEFAULT FALSE,

    -- Entry details
    entry_time TIMESTAMPTZ DEFAULT NOW(),
    up_entry_price DECIMAL(10,4) NOT NULL,
    down_entry_price DECIMAL(10,4) NOT NULL,
    combined_cost DECIMAL(10,4) NOT NULL,
    target_size DECIMAL(10,2) NOT NULL,

    -- Fill tracking
    up_filled DECIMAL(10,2) DEFAULT 0,
    down_filled DECIMAL(10,2) DEFAULT 0,

    -- Status
    status TEXT DEFAULT 'PENDING',

    -- Profit tracking
    expected_profit DECIMAL(10,4),
    realized_profit DECIMAL(10,4),

    -- Timestamps
    market_resolution_time TIMESTAMPTZ,
    merge_time TIMESTAMPTZ,
    profit_realized_time TIMESTAMPTZ,

    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_gabagool_status ON gabagool_positions(status);
CREATE INDEX idx_gabagool_market ON gabagool_positions(market_slug);
```

---

## 7. Profit Realization

### Two Paths to Profit

#### Path A: Immediate Merge (Preferred)

When both YES and NO positions are filled and balanced:

```python
async def execute_merge(self, position: TrackedGabagoolPosition) -> bool:
    """
    Merge balanced YES/NO positions to recover USDC immediately.

    This is the preferred profit realization method because:
    1. Immediate liquidity (don't wait for market resolution)
    2. No resolution risk (market disputes, delayed resolution)
    3. Can reinvest capital in next opportunity

    Merge formula: min(YES, NO) shares → min(YES, NO) × $1.00 USDC
    """
    if position.imbalance > 0:
        self.log(f"Cannot merge: position imbalanced by {position.imbalance}")
        return False

    mergeable_amount = position.min_filled
    if mergeable_amount < 1:  # Minimum merge size
        return False

    # Convert to raw units (USDC has 6 decimals)
    raw_amount = int(mergeable_amount * 1e6)

    try:
        tx_hash = await self.client.merge_positions(
            amount_to_merge=raw_amount,
            condition_id=position.condition_id,
            is_neg_risk_market=position.neg_risk
        )

        position.status = "MERGED"
        position.merge_time = datetime.now(timezone.utc)

        # Calculate realized profit
        usdc_received = mergeable_amount  # $1.00 per merged pair
        cost_basis = position.combined_cost * mergeable_amount
        position.realized_profit = usdc_received - cost_basis

        self.log(f"Merged {mergeable_amount} pairs, profit: ${position.realized_profit:.2f}")
        return True

    except Exception as e:
        self.log(f"Merge failed: {e}")
        return False
```

#### Path B: Wait for Resolution (Fallback)

If positions are imbalanced, wait for market resolution:

```python
async def await_resolution(self, position: TrackedGabagoolPosition) -> bool:
    """
    Wait for market to resolve and redeem winning positions.

    Use this path when:
    1. Position is imbalanced (cannot merge full amount)
    2. Merge fails for technical reasons
    3. Market resolves before merge opportunity

    Note: This path has resolution risk but still guarantees profit
    on the balanced portion of the position.
    """
    # Check if market has resolved
    is_resolved, _ = self.client.is_market_resolved(position.up_token)

    if not is_resolved:
        return False

    # Redeem positions
    try:
        tx_hash = await self.client.redeem_positions(position.condition_id)

        position.status = "RESOLVED"
        position.realized_profit = position.expected_profit  # Approximately

        return True

    except Exception as e:
        self.log(f"Redemption failed: {e}")
        return False
```

### Profit Calculation Formulas

```python
def calculate_profits(position: TrackedGabagoolPosition) -> ProfitSummary:
    """
    Calculate expected and realized profits for a Gabagool position.

    Fee structure (corrected):
    - NO winner fee on Polymarket
    - Only cost is gas (~$0.002 for merge)
    - Maker orders: FREE
    - Taker orders: Up to 1.56% at 50% odds (if used for rescue)
    """
    # Balanced portion (can be merged for guaranteed $1.00)
    balanced_size = position.min_filled
    balanced_cost = balanced_size * position.combined_cost
    balanced_gross = balanced_size * 1.00
    gas_cost = 0.002  # Merge transaction gas
    balanced_net = balanced_gross - balanced_cost - gas_cost  # NO 2% fee!

    # Imbalanced portion (directional exposure)
    imbalance = position.imbalance
    if imbalance > 0:
        # We have more of one side than the other
        # Expected value depends on outcome probability (assume 50%)
        imbalanced_ev = imbalance * 0.50  # Expected value at 50% odds
        imbalanced_cost = imbalance * (position.up_entry_price if position.up_filled > position.down_filled else position.down_entry_price)
        imbalanced_net = imbalanced_ev - imbalanced_cost
    else:
        imbalanced_net = 0

    return ProfitSummary(
        balanced_size=balanced_size,
        balanced_profit=balanced_net,
        imbalanced_size=imbalance,
        imbalanced_ev=imbalanced_net,
        total_expected=balanced_net + imbalanced_net,
        profit_percentage=(balanced_net / balanced_cost * 100) if balanced_cost > 0 else 0
    )
```

---

## 8. Risk Management

### Circuit Breaker System (Ported from Rust Reference)

**Source**: This pattern is adapted from the production Rust arbitrage bot in `/sampatt/cledo/reference/`.

The circuit breaker is a critical safety system that automatically halts trading when risk thresholds are exceeded. This prevents catastrophic losses from bugs, market anomalies, or cascading errors.

```python
@dataclass
class CircuitBreakerConfig:
    """
    Circuit breaker configuration.

    Ported from Rust reference implementation which uses these limits
    for production cross-platform arbitrage trading.
    """
    # Position limits
    max_position_per_market: float = 500.0    # Max $ exposure per market
    max_total_position: float = 2000.0        # Max $ total exposure across all markets

    # Loss limits
    max_daily_loss: float = 100.0             # Max $ loss before halt (conservative for Gabagool)
    max_loss_per_trade: float = 20.0          # Max $ loss on single trade

    # Error limits
    max_consecutive_errors: int = 5           # Halt after N consecutive failures
    max_errors_per_hour: int = 20             # Halt if too many errors in 1 hour

    # Timing
    cooldown_seconds: int = 300               # 5 minute cooldown after halt

    # Auto-recovery
    auto_recover: bool = True                 # Automatically resume after cooldown
    require_manual_reset: bool = False        # Require human intervention to resume


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
        daily_pnl_reset_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

        # Error tracking
        consecutive_errors: int = 0
        errors_this_hour: int = 0
        error_hour_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

        # Metrics
        total_trades: int = 0
        successful_trades: int = 0
        failed_trades: int = 0

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
                return False, f"Would exceed market position limit: ${new_market_position:.2f} > ${self.config.max_position_per_market:.2f}"

            new_total_position = self.state.total_position + size
            if new_total_position > self.config.max_total_position:
                return False, f"Would exceed total position limit: ${new_total_position:.2f} > ${self.config.max_total_position:.2f}"

            # Check daily loss limit
            if self.state.daily_pnl < -self.config.max_daily_loss:
                await self._halt(f"Daily loss limit exceeded: ${-self.state.daily_pnl:.2f}")
                return False, self.state.halt_reason

            return True, "OK"

    async def record_trade_result(
        self,
        market_id: str,
        size: float,
        pnl: float,
        success: bool,
        error_msg: str = ""
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

                # Check consecutive error limit
                if self.state.consecutive_errors >= self.config.max_consecutive_errors:
                    await self._halt(f"Too many consecutive errors: {self.state.consecutive_errors}")

                # Check hourly error limit
                if self.state.errors_this_hour >= self.config.max_errors_per_hour:
                    await self._halt(f"Too many errors this hour: {self.state.errors_this_hour}")

    async def record_position_closed(self, market_id: str, size: float, pnl: float):
        """Record when a position is closed (merged or resolved)."""
        async with self._lock:
            current = self.state.positions_by_market.get(market_id, 0.0)
            self.state.positions_by_market[market_id] = max(0, current - size)
            self.state.total_position = max(0, self.state.total_position - size)
            self.state.daily_pnl += pnl

    async def _halt(self, reason: str):
        """Halt the circuit breaker."""
        self.state.is_halted = True
        self.state.halt_reason = reason
        self.state.halt_time = datetime.now(timezone.utc)

        # Send alert
        logging.critical(f"CIRCUIT BREAKER HALTED: {reason}")
        # TODO: Send Telegram alert

    async def _reset(self):
        """Reset the circuit breaker after cooldown."""
        self.state.is_halted = False
        self.state.halt_reason = ""
        self.state.halt_time = None
        self.state.consecutive_errors = 0
        logging.info("Circuit breaker reset - trading resumed")

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
            "daily_pnl": self.state.daily_pnl,
            "consecutive_errors": self.state.consecutive_errors,
            "total_trades": self.state.total_trades,
            "success_rate": self.state.successful_trades / max(1, self.state.total_trades) * 100
        }
```

---

### Risk 1: Partial Fills (Critical) - Position Reconciliation

**Problem**: One side fills, other doesn't → directional exposure

**Solution**: Automatic position reconciliation (ported from Rust reference)

The Rust reference implementation uses an "auto-close" pattern that automatically sells excess positions in the background. This ensures the bot always returns to a market-neutral state.

```python
class PositionReconciler:
    """
    Automatic position reconciliation for Gabagool strategy.

    When fills don't match (e.g., bought 100 UP but only 80 DOWN),
    this system automatically closes the excess to maintain neutrality.

    Ported from Rust reference: src/execution.rs (auto_close_mismatch)
    """

    def __init__(self, client, circuit_breaker: CircuitBreaker):
        self.client = client
        self.circuit_breaker = circuit_breaker
        self.pending_reconciliations: Dict[str, asyncio.Task] = {}
        self.reconciliation_delay: float = 2.0  # seconds to wait before auto-close

    async def check_and_reconcile(
        self,
        position: TrackedGabagoolPosition
    ) -> ReconciliationResult:
        """
        Check if position needs reconciliation and handle it.

        Called after order execution to detect and fix mismatches.

        Flow (from Rust reference):
        1. Compare UP and DOWN fill sizes
        2. If mismatch > threshold, spawn background auto-close task
        3. Wait reconciliation_delay seconds (let market settle)
        4. Sell excess on the side that over-filled
        5. Update position tracking
        """
        imbalance = abs(position.up_filled - position.down_filled)

        # Small imbalances are OK (rounding, dust)
        if imbalance < 1.0:
            return ReconciliationResult(needed=False)

        # Determine which side has excess
        if position.up_filled > position.down_filled:
            excess_side = "UP"
            excess_token = position.up_token
            excess_size = position.up_filled - position.down_filled
        else:
            excess_side = "DOWN"
            excess_token = position.down_token
            excess_size = position.down_filled - position.up_filled

        # Spawn background reconciliation task
        task_key = f"{position.id}_{excess_side}"
        if task_key not in self.pending_reconciliations:
            task = asyncio.create_task(
                self._auto_close_excess(position, excess_side, excess_token, excess_size)
            )
            self.pending_reconciliations[task_key] = task

            return ReconciliationResult(
                needed=True,
                excess_side=excess_side,
                excess_size=excess_size,
                task_spawned=True
            )

        return ReconciliationResult(needed=True, already_pending=True)

    async def _auto_close_excess(
        self,
        position: TrackedGabagoolPosition,
        excess_side: str,
        excess_token: str,
        excess_size: float
    ):
        """
        Background task to close excess position.

        From Rust reference: Waits 2 seconds, then sells excess at market.
        This delay allows the market to settle and avoids selling into
        temporary price dislocations.
        """
        task_key = f"{position.id}_{excess_side}"

        try:
            # Wait for market to settle (from Rust: 2 second delay)
            logging.info(f"Auto-close scheduled: {excess_side} {excess_size} in {self.reconciliation_delay}s")
            await asyncio.sleep(self.reconciliation_delay)

            # Get current best bid to sell into
            best_bid = await self._get_best_bid(excess_token)

            if best_bid is None:
                logging.warning(f"Auto-close failed: no bids for {excess_side}")
                return

            # Place sell order (IOC/FAK style - immediate execution)
            logging.info(f"Auto-close executing: SELL {excess_size} {excess_side} @ {best_bid}")

            result = await self.client.create_order(
                marketId=excess_token,
                action="SELL",
                price=best_bid,
                size=excess_size,
                neg_risk=position.neg_risk,
                post_only=False  # Allow immediate fill
            )

            if result and result.get("success"):
                filled = float(result.get("size_matched", 0))
                sell_value = filled * best_bid

                # Calculate P&L impact
                entry_price = position.up_entry_price if excess_side == "UP" else position.down_entry_price
                cost_basis = filled * entry_price
                pnl = sell_value - cost_basis

                logging.info(f"Auto-close complete: sold {filled} @ {best_bid}, P&L: ${pnl:.2f}")

                # Update circuit breaker
                await self.circuit_breaker.record_position_closed(
                    position.market_slug, filled, pnl
                )

                # Update position tracking
                if excess_side == "UP":
                    position.up_filled -= filled
                else:
                    position.down_filled -= filled
            else:
                error_msg = result.get("errorMsg", "Unknown error") if result else "No response"
                logging.warning(f"Auto-close failed: {error_msg}")

        except Exception as e:
            logging.error(f"Auto-close error: {e}")

        finally:
            # Clean up pending task
            self.pending_reconciliations.pop(task_key, None)

    async def _get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid price for selling."""
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bids = data.get("bids", [])
                        if bids:
                            sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
                            return float(sorted_bids[0]["price"])
        except Exception as e:
            logging.error(f"Error fetching best bid: {e}")
        return None


@dataclass
class ReconciliationResult:
    """Result of position reconciliation check."""
    needed: bool
    excess_side: str = ""
    excess_size: float = 0.0
    task_spawned: bool = False
    already_pending: bool = False
```

---

### Risk 1b: Partial Fill Rescue (Original Strategy)

In addition to automatic reconciliation, we still need active rescue attempts:

```python
class PartialFillHandler:
    """
    Handle partial fill scenarios to minimize directional exposure.

    Works alongside PositionReconciler:
    - PartialFillHandler: Active rescue attempts (try to complete the trade)
    - PositionReconciler: Passive cleanup (sell excess if rescue fails)
    """

    def __init__(self, client, reconciler: PositionReconciler):
        self.client = client
        self.reconciler = reconciler

    async def handle_partial_fill(
        self,
        position: TrackedGabagoolPosition,
        filled_side: str,  # "UP" or "DOWN"
        unfilled_token: str
    ) -> RescueResult:
        """
        Rescue a partially filled position.

        Strategy priority:
        1. Aggressive maker order on unfilled side
        2. Taker order if time running out
        3. Immediate exit if spread inverted
        4. Fall back to auto-reconciliation
        """
        # Check current spread
        current_ask = self.get_best_ask(unfilled_token)

        if current_ask is None:
            # Can't complete - trigger reconciliation
            await self.reconciler.check_and_reconcile(position)
            return RescueResult(success=False, action="RECONCILE")

        # Calculate if still profitable to complete
        filled_price = position.up_entry_price if filled_side == "UP" else position.down_entry_price
        new_combined = filled_price + current_ask

        if new_combined >= 1.00:
            # No longer profitable - trigger reconciliation to exit
            await self.reconciler.check_and_reconcile(position)
            return RescueResult(success=False, action="RECONCILE", reason="Spread inverted")

        # Still profitable - try to complete
        time_remaining = (position.market_resolution_time - datetime.now(timezone.utc)).total_seconds()

        if time_remaining < 30:
            # Use taker for speed
            return await self.taker_rescue(unfilled_token, position.target_size, current_ask)
        else:
            # Use aggressive maker
            return await self.maker_rescue(unfilled_token, position.target_size, current_ask - 0.01)

    async def taker_rescue(self, token_id: str, size: float, price: float) -> RescueResult:
        """Place taker order to immediately fill unfilled side."""
        result = await self.client.create_order(
            marketId=token_id,
            action="BUY",
            price=price,
            size=size,
            post_only=False  # Allow taker
        )

        if result and result.get("success"):
            return RescueResult(success=True, action="TAKER_RESCUE")
        return RescueResult(success=False, action="TAKER_RESCUE_FAILED")

    async def maker_rescue(self, token_id: str, size: float, price: float) -> RescueResult:
        """Place aggressive maker order on unfilled side."""
        result = await self.client.create_order(
            marketId=token_id,
            action="BUY",
            price=price,
            size=size,
            post_only=True
        )

        if result and result.get("success"):
            return RescueResult(success=True, action="MAKER_RESCUE")
        return RescueResult(success=False, action="MAKER_RESCUE_FAILED")
```

### Risk 2: Execution Timing

**Problem**: Opportunity disappears before both orders fill

**Mitigation**:

```python
TIMING_RULES = {
    # Don't enter new positions if market starts within this time
    "min_time_to_start": 60,  # seconds

    # Switch from maker to taker if this close to market start
    "taker_threshold": 30,  # seconds

    # Maximum time to wait for fills before canceling
    "max_fill_wait": 45,  # seconds

    # Minimum time between opportunity scans
    "scan_interval": 1,  # seconds
}

def should_execute(opportunity: Opportunity, market: dict) -> Tuple[bool, str]:
    """
    Determine if we should execute on an opportunity.
    """
    time_to_start = (market["_event_start"] - datetime.now(timezone.utc)).total_seconds()

    if time_to_start < TIMING_RULES["min_time_to_start"]:
        return False, f"Too close to start ({time_to_start:.0f}s)"

    if opportunity.max_size < 10:
        return False, f"Insufficient liquidity ({opportunity.max_size} shares)"

    if opportunity.net_profit_pct < 0.5:
        return False, f"Profit too small ({opportunity.net_profit_pct:.2f}%)"

    return True, "OK"
```

### Risk 3: Gas Costs

**Problem**: High gas during Polygon congestion can eat profits

**Mitigation**:

```python
async def check_gas_profitability(
    position_size: float,
    expected_profit: float
) -> Tuple[bool, float]:
    """
    Verify gas costs don't exceed expected profit.
    """
    # Get current gas price
    gas_price = await get_polygon_gas_price()

    # Estimate gas for two orders + one merge
    estimated_gas_units = 500_000  # Conservative estimate
    gas_cost_matic = gas_price * estimated_gas_units / 1e18

    # Convert MATIC to USD (fetch current price)
    matic_price = await get_matic_price()
    gas_cost_usd = gas_cost_matic * matic_price

    # Check if profitable after gas
    net_after_gas = expected_profit - gas_cost_usd

    if net_after_gas < 0.10:  # Minimum profit threshold
        return False, gas_cost_usd

    return True, gas_cost_usd
```

### Risk 4: Order Book Manipulation (Spoofing)

**Problem**: Fake orders placed to bait bots

**Mitigation** (already implemented in strategy.py):

```python
# Use VWAP instead of best price
# Filter out small orders (<$5 value)
# Consider multiple levels of the book
# Set minimum order size requirements
```

### Risk 5: Market Resolution Disputes

**Problem**: Polymarket may not resolve markets as expected

**Mitigation**:

```python
# 1. Prefer merge over resolution (bypass oracle)
# 2. Set maximum position size per market
# 3. Track resolution disputes and avoid problematic markets
# 4. Diversify across multiple markets
```

---

## 9. Configuration Parameters

### New Configuration File: `rebates/gabagool/config.py`

```python
"""
Configuration for Gabagool arbitrage strategy.

Fee structure (15-minute crypto markets):
- Maker orders: FREE (no fees, plus earn rebates!)
- Taker orders: Up to 1.56% at 50% odds
- Winner fee: NONE (no fee on profits!)
- Gas: ~$0.002 per merge transaction
"""
import os

# ============== PROFIT THRESHOLDS ==============

# Maximum combined cost (YES + NO) to consider profitable
# Lower = more conservative, higher = more opportunities
#
# With NO winner fee, thresholds are much tighter:
# - Maker only: 0.99 (1% spread, ~$1 profit per $100)
# - Taker rescue: 0.98 (2% spread to cover 1.56% taker fee)
# - Conservative: 0.985 (1.5% spread, good balance)
PROFIT_THRESHOLD = float(os.getenv("GABAGOOL_PROFIT_THRESHOLD", "0.99"))

# Minimum net profit percentage to execute
# With no winner fee, even 0.5% is meaningful profit
MIN_NET_PROFIT_PCT = float(os.getenv("GABAGOOL_MIN_NET_PROFIT", "0.5"))

# ============== POSITION SIZING ==============

# Trade size per opportunity (USDC)
TRADE_SIZE = float(os.getenv("GABAGOOL_TRADE_SIZE", "50"))

# Maximum position per market (USDC)
MAX_POSITION_PER_MARKET = float(os.getenv("GABAGOOL_MAX_POSITION", "200"))

# Minimum order size (Polymarket minimum is 5)
MIN_ORDER_SIZE = float(os.getenv("GABAGOOL_MIN_ORDER", "10"))

# ============== EXECUTION ==============

# Order type preference: "MAKER", "TAKER", or "HYBRID"
EXECUTION_MODE = os.getenv("GABAGOOL_EXECUTION_MODE", "HYBRID")

# Aggression for maker orders (0.0 = conservative, 1.0 = aggressive)
MAKER_AGGRESSION = float(os.getenv("GABAGOOL_MAKER_AGGRESSION", "0.5"))

# Maximum price to pay on taker orders
MAX_TAKER_PRICE = float(os.getenv("GABAGOOL_MAX_TAKER_PRICE", "0.55"))

# ============== TIMING ==============

# Minimum seconds before market start to enter
MIN_TIME_TO_START = int(os.getenv("GABAGOOL_MIN_TIME", "60"))

# Time threshold to switch from maker to taker
TAKER_SWITCH_TIME = int(os.getenv("GABAGOOL_TAKER_SWITCH", "30"))

# Maximum time to wait for fills (seconds)
MAX_FILL_WAIT = int(os.getenv("GABAGOOL_MAX_FILL_WAIT", "45"))

# Scan interval for opportunities (seconds)
SCAN_INTERVAL = float(os.getenv("GABAGOOL_SCAN_INTERVAL", "1.0"))

# ============== RISK MANAGEMENT ==============

# Maximum number of concurrent positions
MAX_CONCURRENT_POSITIONS = int(os.getenv("GABAGOOL_MAX_CONCURRENT", "5"))

# Maximum imbalance tolerance before emergency exit
MAX_IMBALANCE_PCT = float(os.getenv("GABAGOOL_MAX_IMBALANCE", "20"))

# Minimum liquidity required (shares)
MIN_LIQUIDITY = float(os.getenv("GABAGOOL_MIN_LIQUIDITY", "50"))

# Maximum gas cost (USD) to allow execution
MAX_GAS_COST = float(os.getenv("GABAGOOL_MAX_GAS", "0.50"))

# ============== CIRCUIT BREAKER (from Rust reference) ==============

# Position limits
CB_MAX_POSITION_PER_MARKET = float(os.getenv("GABAGOOL_CB_MAX_POS_MARKET", "500"))
CB_MAX_TOTAL_POSITION = float(os.getenv("GABAGOOL_CB_MAX_POS_TOTAL", "2000"))

# Loss limits
CB_MAX_DAILY_LOSS = float(os.getenv("GABAGOOL_CB_MAX_DAILY_LOSS", "100"))
CB_MAX_LOSS_PER_TRADE = float(os.getenv("GABAGOOL_CB_MAX_LOSS_TRADE", "20"))

# Error limits
CB_MAX_CONSECUTIVE_ERRORS = int(os.getenv("GABAGOOL_CB_MAX_CONSEC_ERRORS", "5"))
CB_MAX_ERRORS_PER_HOUR = int(os.getenv("GABAGOOL_CB_MAX_ERRORS_HOUR", "20"))

# Timing
CB_COOLDOWN_SECONDS = int(os.getenv("GABAGOOL_CB_COOLDOWN", "300"))

# Recovery
CB_AUTO_RECOVER = os.getenv("GABAGOOL_CB_AUTO_RECOVER", "true").lower() == "true"
CB_REQUIRE_MANUAL_RESET = os.getenv("GABAGOOL_CB_MANUAL_RESET", "false").lower() == "true"

# ============== POSITION RECONCILIATION (from Rust reference) ==============

# Delay before auto-closing excess positions (seconds)
RECONCILIATION_DELAY = float(os.getenv("GABAGOOL_RECONCILE_DELAY", "2.0"))

# Minimum imbalance to trigger reconciliation (shares)
RECONCILIATION_MIN_IMBALANCE = float(os.getenv("GABAGOOL_RECONCILE_MIN", "1.0"))

# ============== MODE ==============

# Enable Gabagool strategy (can run alongside rebates)
ENABLED = os.getenv("GABAGOOL_ENABLED", "true").lower() == "true"

# Dry run mode (no real trades)
DRY_RUN = os.getenv("GABAGOOL_DRY_RUN", "true").lower() == "true"
```

### Environment Variables Summary

```bash
# === Gabagool Strategy Configuration ===
# NOTE: No winner fee on Polymarket! Only taker fees on 15-min crypto markets.

# Enable/disable
GABAGOOL_ENABLED=true
GABAGOOL_DRY_RUN=true  # Set to false for live trading

# Profit thresholds (tighter now with no winner fee!)
GABAGOOL_PROFIT_THRESHOLD=0.99  # Combined YES+NO must be below this (1% spread)
GABAGOOL_MIN_NET_PROFIT=0.5     # Minimum profit % (just need to cover gas)

# Position sizing
GABAGOOL_TRADE_SIZE=50          # USDC per opportunity
GABAGOOL_MAX_POSITION=200       # Max USDC per market
GABAGOOL_MIN_ORDER=10           # Minimum order size

# Execution
GABAGOOL_EXECUTION_MODE=HYBRID  # MAKER, TAKER, or HYBRID
GABAGOOL_MAKER_AGGRESSION=0.5   # How aggressive on maker pricing
GABAGOOL_MAX_TAKER_PRICE=0.55   # Cap on taker price

# Timing
GABAGOOL_MIN_TIME=60            # Min seconds before market start
GABAGOOL_TAKER_SWITCH=30        # Switch to taker when this close
GABAGOOL_SCAN_INTERVAL=1.0      # Seconds between scans

# Risk management
GABAGOOL_MAX_CONCURRENT=5       # Max simultaneous positions
GABAGOOL_MIN_LIQUIDITY=50       # Min shares available
GABAGOOL_MAX_GAS=0.50           # Max gas cost in USD

# Circuit breaker (ported from Rust reference)
GABAGOOL_CB_MAX_POS_MARKET=500      # Max $ per market
GABAGOOL_CB_MAX_POS_TOTAL=2000      # Max $ total exposure
GABAGOOL_CB_MAX_DAILY_LOSS=100      # Halt if daily loss exceeds this
GABAGOOL_CB_MAX_LOSS_TRADE=20       # Halt if single trade loses this much
GABAGOOL_CB_MAX_CONSEC_ERRORS=5     # Halt after N consecutive errors
GABAGOOL_CB_MAX_ERRORS_HOUR=20      # Halt if N errors in 1 hour
GABAGOOL_CB_COOLDOWN=300            # Seconds to wait before auto-resume
GABAGOOL_CB_AUTO_RECOVER=true       # Auto-resume after cooldown
GABAGOOL_CB_MANUAL_RESET=false      # Require manual intervention

# Position reconciliation (ported from Rust reference)
GABAGOOL_RECONCILE_DELAY=2.0        # Seconds before auto-closing excess
GABAGOOL_RECONCILE_MIN=1.0          # Min imbalance to trigger reconciliation
```

---

## 10. Implementation Phases

### Phase 1: Scanner Foundation + Circuit Breaker

**Goal**: Build opportunity detection and safety systems

**Deliverables**:
1. `gabagool/scanner.py` - Orderbook scanning
2. `gabagool/config.py` - Configuration (including circuit breaker)
3. `gabagool/circuit_breaker.py` - Circuit breaker system (from Rust reference)
4. Logging of detected opportunities
5. Integration with existing market finder

**Tasks**:
- [ ] Create `rebates/gabagool/` directory structure
- [ ] Implement `GabagoolScanner` class
- [ ] Add VWAP-based price calculation
- [ ] **Implement `CircuitBreaker` class (ported from Rust)**
- [ ] **Implement `CircuitBreakerConfig` dataclass**
- [ ] Create opportunity detection loop
- [ ] Add opportunity logging/metrics
- [ ] Test with dry run mode

**Success Criteria**:
- Scanner correctly identifies opportunities when YES+NO < threshold
- False positive rate < 5%
- Scan latency < 500ms per market
- **Circuit breaker halts on position limit exceeded**
- **Circuit breaker halts on daily loss limit**
- **Circuit breaker halts on consecutive errors**

### Phase 2: Execution Engine + Position Reconciliation

**Goal**: Implement order execution with automatic position reconciliation

**Deliverables**:
1. `gabagool/executor.py` - Order execution logic
2. `gabagool/reconciler.py` - Position reconciliation (from Rust reference)
3. Fill tracking and monitoring
4. Partial fill handling with auto-close

**Tasks**:
- [ ] Implement `GabagoolExecutor` class
- [ ] Add simultaneous order placement
- [ ] Implement fill monitoring
- [ ] **Implement `PositionReconciler` class (ported from Rust)**
- [ ] **Add auto-close background task for mismatched fills**
- [ ] **Integrate reconciler with circuit breaker**
- [ ] Add partial fill rescue logic (PartialFillHandler)
- [ ] Create emergency exit procedures
- [ ] Test execution in dry run mode

**Success Criteria**:
- Both orders placed within 1 second
- Fill detection latency < 2 seconds
- Partial fill rescue success rate > 90%
- **Mismatched fills auto-closed within 5 seconds**
- **Reconciliation updates circuit breaker positions**

### Phase 3: Position Management

**Goal**: Track positions and realize profits

**Deliverables**:
1. `gabagool/position_tracker.py` - Position state machine
2. Database schema and persistence
3. Merge execution integration

**Tasks**:
- [ ] Implement `TrackedGabagoolPosition` dataclass
- [ ] Create position state machine
- [ ] Add database persistence
- [ ] Integrate with existing merge functionality
- [ ] Add profit calculation and tracking
- [ ] **Integrate with circuit breaker for position updates**
- [ ] Test position lifecycle

**Success Criteria**:
- Position state transitions correctly
- Merge execution succeeds > 95%
- Profit tracking accurate within 0.1%
- **Circuit breaker position tracking matches actual positions**

### Phase 4: Integration & Testing

**Goal**: Integrate with rebates bot and test end-to-end

**Deliverables**:
1. Modified `rebates_bot.py` with Gabagool mode
2. Telegram alerts for Gabagool events (including circuit breaker)
3. Comprehensive test suite

**Tasks**:
- [ ] Integrate Gabagool into main bot loop
- [ ] Add strategy selection logic
- [ ] Create Telegram alert functions
- [ ] **Add circuit breaker status to Telegram alerts**
- [ ] **Add reconciliation alerts to Telegram**
- [ ] Write unit tests (including circuit breaker tests)
- [ ] Write integration tests
- [ ] Perform dry run testing
- [ ] **Test circuit breaker halt and recovery**
- [ ] **Test position reconciliation edge cases**

**Success Criteria**:
- Bot runs stably for 24+ hours
- All tests pass
- Alerts working correctly
- **Circuit breaker correctly halts and resumes**
- **No orphaned positions after 24-hour run**

### Phase 5: Live Deployment

**Goal**: Deploy to production with monitoring

**Deliverables**:
1. Production deployment
2. Monitoring dashboard updates (including circuit breaker status)
3. Documentation

**Tasks**:
- [ ] Start with minimal capital ($100)
- [ ] **Set conservative circuit breaker limits initially**
- [ ] Monitor for 48 hours
- [ ] **Review circuit breaker logs and adjust limits**
- [ ] Scale up gradually
- [ ] Document operational procedures
- [ ] Create runbook for issues
- [ ] **Document circuit breaker override procedures**

**Success Criteria**:
- Profitable operation for 48+ hours
- No critical bugs
- Monitoring and alerts functional
- **Zero unreconciled positions**
- **Circuit breaker never triggered unexpectedly**

---

## 11. Testing Strategy

### Unit Tests

```python
# tests/unit/test_gabagool_scanner.py

class TestGabagoolScanner:
    """Test opportunity detection logic."""

    def test_detects_profitable_opportunity(self):
        """Verify scanner finds opportunities when YES+NO < threshold."""
        scanner = GabagoolScanner(profit_threshold=0.99)  # 1% spread threshold

        # Mock orderbook with profitable spread (2% = very profitable)
        mock_up_book = {"asks": [{"price": "0.49", "size": "100"}]}
        mock_down_book = {"asks": [{"price": "0.49", "size": "100"}]}

        with patch.object(scanner, 'get_orderbook', side_effect=[mock_up_book, mock_down_book]):
            opportunity = scanner.scan_market("up_token", "down_token")

        assert opportunity is not None
        assert opportunity.combined_cost == 0.98
        assert opportunity.net_profit_pct > 0  # ~2% profit with no winner fee!

    def test_rejects_unprofitable_spread(self):
        """Verify scanner ignores opportunities above threshold."""
        scanner = GabagoolScanner(profit_threshold=0.99)  # 1% spread threshold

        # Combined = 1.00, no profit
        mock_up_book = {"asks": [{"price": "0.50", "size": "100"}]}
        mock_down_book = {"asks": [{"price": "0.50", "size": "100"}]}

        with patch.object(scanner, 'get_orderbook', side_effect=[mock_up_book, mock_down_book]):
            opportunity = scanner.scan_market("up_token", "down_token")

        assert opportunity is None

    def test_respects_minimum_liquidity(self):
        """Verify scanner requires minimum liquidity."""
        scanner = GabagoolScanner(profit_threshold=0.99)
        scanner.min_liquidity = 50

        mock_up_book = {"asks": [{"price": "0.48", "size": "10"}]}  # Only 10 shares
        mock_down_book = {"asks": [{"price": "0.48", "size": "100"}]}

        with patch.object(scanner, 'get_orderbook', side_effect=[mock_up_book, mock_down_book]):
            opportunity = scanner.scan_market("up_token", "down_token")

        assert opportunity is None


# tests/unit/test_circuit_breaker.py

class TestCircuitBreaker:
    """Test circuit breaker safety logic (ported from Rust reference)."""

    @pytest.fixture
    def circuit_breaker(self):
        config = CircuitBreakerConfig(
            max_position_per_market=100.0,
            max_total_position=500.0,
            max_daily_loss=50.0,
            max_consecutive_errors=3
        )
        return CircuitBreaker(config)

    @pytest.mark.asyncio
    async def test_allows_trade_within_limits(self, circuit_breaker):
        """Verify trades allowed when within all limits."""
        allowed, reason = await circuit_breaker.check_can_trade("market_1", 50.0)
        assert allowed is True
        assert reason == "OK"

    @pytest.mark.asyncio
    async def test_blocks_trade_exceeding_market_limit(self, circuit_breaker):
        """Verify trade blocked when exceeding per-market position limit."""
        # First trade OK
        await circuit_breaker.record_trade_result("market_1", 80.0, 1.0, success=True)

        # Second trade would exceed limit
        allowed, reason = await circuit_breaker.check_can_trade("market_1", 30.0)
        assert allowed is False
        assert "market position limit" in reason

    @pytest.mark.asyncio
    async def test_blocks_trade_exceeding_total_limit(self, circuit_breaker):
        """Verify trade blocked when exceeding total position limit."""
        # Fill up multiple markets
        await circuit_breaker.record_trade_result("market_1", 100.0, 1.0, success=True)
        await circuit_breaker.record_trade_result("market_2", 100.0, 1.0, success=True)
        await circuit_breaker.record_trade_result("market_3", 100.0, 1.0, success=True)
        await circuit_breaker.record_trade_result("market_4", 100.0, 1.0, success=True)
        await circuit_breaker.record_trade_result("market_5", 100.0, 1.0, success=True)

        # Next trade would exceed total limit
        allowed, reason = await circuit_breaker.check_can_trade("market_6", 50.0)
        assert allowed is False
        assert "total position limit" in reason

    @pytest.mark.asyncio
    async def test_halts_on_daily_loss_limit(self, circuit_breaker):
        """Verify circuit breaker halts when daily loss limit exceeded."""
        # Record losing trades
        await circuit_breaker.record_trade_result("market_1", 50.0, -30.0, success=True)
        await circuit_breaker.record_trade_result("market_2", 50.0, -25.0, success=True)

        # Should be halted now
        allowed, reason = await circuit_breaker.check_can_trade("market_3", 10.0)
        assert allowed is False
        assert "halted" in reason.lower()

    @pytest.mark.asyncio
    async def test_halts_on_consecutive_errors(self, circuit_breaker):
        """Verify circuit breaker halts after consecutive errors."""
        # Record 3 consecutive errors
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)

        # Should be halted now
        allowed, reason = await circuit_breaker.check_can_trade("market_1", 10.0)
        assert allowed is False
        assert "consecutive errors" in reason.lower()

    @pytest.mark.asyncio
    async def test_resets_error_count_on_success(self, circuit_breaker):
        """Verify successful trade resets consecutive error count."""
        # Record 2 errors
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)

        # Success should reset
        await circuit_breaker.record_trade_result("market_1", 50.0, 1.0, success=True)

        # 2 more errors should not trigger halt (reset happened)
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)
        await circuit_breaker.record_trade_result("market_1", 50.0, 0, success=False)

        allowed, _ = await circuit_breaker.check_can_trade("market_1", 10.0)
        assert allowed is True  # Not halted yet


# tests/unit/test_position_reconciler.py

class TestPositionReconciler:
    """Test position reconciliation logic (ported from Rust reference)."""

    @pytest.fixture
    def mock_position(self):
        return TrackedGabagoolPosition(
            id="test_pos_1",
            market_slug="test-market",
            condition_id="0x123",
            up_token="up_token_123",
            down_token="down_token_456",
            neg_risk=False,
            entry_time=datetime.now(timezone.utc),
            up_entry_price=0.48,
            down_entry_price=0.48,
            combined_cost=0.96,
            position_size=100.0,
            up_filled=100.0,
            down_filled=80.0  # Imbalanced!
        )

    @pytest.mark.asyncio
    async def test_detects_imbalanced_position(self, mock_position):
        """Verify reconciler detects imbalanced fills."""
        reconciler = PositionReconciler(mock_client, mock_circuit_breaker)

        result = await reconciler.check_and_reconcile(mock_position)

        assert result.needed is True
        assert result.excess_side == "UP"
        assert result.excess_size == 20.0

    @pytest.mark.asyncio
    async def test_ignores_small_imbalance(self):
        """Verify reconciler ignores dust amounts."""
        position = TrackedGabagoolPosition(
            # ... setup with up_filled=100.0, down_filled=99.5
            up_filled=100.0,
            down_filled=99.5  # Only 0.5 imbalance
        )

        reconciler = PositionReconciler(mock_client, mock_circuit_breaker)
        result = await reconciler.check_and_reconcile(position)

        assert result.needed is False

    @pytest.mark.asyncio
    async def test_spawns_background_task(self, mock_position):
        """Verify auto-close task is spawned for imbalanced position."""
        reconciler = PositionReconciler(mock_client, mock_circuit_breaker)

        result = await reconciler.check_and_reconcile(mock_position)

        assert result.task_spawned is True
        assert f"{mock_position.id}_UP" in reconciler.pending_reconciliations
```

### Integration Tests

```python
# tests/integration/test_gabagool_execution.py

class TestGabagoolExecution:
    """Integration tests for Gabagool execution (requires API credentials)."""

    @pytest.mark.skipif(not os.getenv("POLY_TEST_INTEGRATION"), reason="Integration tests disabled")
    async def test_simultaneous_order_placement(self):
        """Verify both orders are placed within acceptable timeframe."""
        executor = GabagoolExecutor(client)

        start = time.time()
        result = await executor.execute_maker(
            up_token="test_up",
            down_token="test_down",
            size=5,
            opportunity=mock_opportunity
        )
        elapsed = time.time() - start

        assert elapsed < 2.0  # Both orders placed within 2 seconds

    @pytest.mark.skipif(not os.getenv("POLY_TEST_INTEGRATION"), reason="Integration tests disabled")
    async def test_partial_fill_rescue(self):
        """Verify partial fill rescue works correctly."""
        # This test requires careful setup to simulate partial fills
        pass
```

### Dry Run Testing Protocol

```markdown
## Dry Run Testing Checklist

### Day 1: Scanner Validation
- [ ] Run scanner for 4 hours
- [ ] Log all detected opportunities
- [ ] Verify opportunity calculations manually
- [ ] Check false positive rate
- [ ] Document opportunity frequency by time of day

### Day 2: Execution Validation
- [ ] Enable dry run execution
- [ ] Verify order parameters look correct
- [ ] Check timing logic
- [ ] Validate partial fill handling
- [ ] Test emergency exit scenarios

### Day 3: Full System Test
- [ ] Run complete system for 8 hours
- [ ] Monitor logs for errors
- [ ] Verify state machine transitions
- [ ] Check database persistence
- [ ] Test restart recovery

### Day 4-5: Extended Validation
- [ ] 48-hour continuous dry run
- [ ] Analyze profitability projections
- [ ] Stress test with high opportunity frequency
- [ ] Validate all Telegram alerts
- [ ] Review logs for any issues
```

---

## 12. Monitoring & Alerting

### New Telegram Alerts

```python
# alerts/telegram.py - Add these functions

def send_gabagool_opportunity_alert(
    question: str,
    combined_cost: float,
    net_profit_pct: float,
    size: float,
    dry_run: bool = True
) -> bool:
    """Alert when Gabagool opportunity detected."""
    prefix = "[DRY RUN] " if dry_run else ""
    message = (
        f"{prefix}🎯 GABAGOOL OPPORTUNITY\n"
        f"Market: {question[:50]}...\n"
        f"Combined cost: ${combined_cost:.3f}\n"
        f"Net profit: {net_profit_pct:.2f}%\n"
        f"Size: ${size:.2f}"
    )
    return send_message(message)

def send_gabagool_execution_alert(
    question: str,
    up_price: float,
    down_price: float,
    size: float,
    expected_profit: float,
    dry_run: bool = True
) -> bool:
    """Alert when Gabagool orders executed."""
    prefix = "[DRY RUN] " if dry_run else ""
    message = (
        f"{prefix}✅ GABAGOOL EXECUTED\n"
        f"Market: {question[:50]}...\n"
        f"UP @ ${up_price:.3f}\n"
        f"DOWN @ ${down_price:.3f}\n"
        f"Size: ${size:.2f}\n"
        f"Expected profit: ${expected_profit:.2f}"
    )
    return send_message(message)

def send_gabagool_merge_alert(
    question: str,
    merged_size: float,
    realized_profit: float,
    dry_run: bool = True
) -> bool:
    """Alert when positions merged successfully."""
    prefix = "[DRY RUN] " if dry_run else ""
    message = (
        f"{prefix}💰 GABAGOOL MERGED\n"
        f"Market: {question[:50]}...\n"
        f"Merged: ${merged_size:.2f}\n"
        f"Profit: ${realized_profit:.2f}"
    )
    return send_message(message)

def send_gabagool_partial_fill_alert(
    question: str,
    filled_side: str,
    filled_size: float,
    action: str,
    dry_run: bool = True
) -> bool:
    """Alert on partial fill situation."""
    prefix = "[DRY RUN] " if dry_run else ""
    message = (
        f"{prefix}⚠️ GABAGOOL PARTIAL FILL\n"
        f"Market: {question[:50]}...\n"
        f"Filled: {filled_side} ${filled_size:.2f}\n"
        f"Action: {action}"
    )
    return send_message(message)
```

### Metrics to Track

```python
@dataclass
class GabagoolMetrics:
    """Metrics for Gabagool strategy performance."""

    # Opportunity metrics
    opportunities_detected: int = 0
    opportunities_executed: int = 0
    opportunities_skipped: int = 0

    # Execution metrics
    both_sides_filled: int = 0
    partial_fills: int = 0
    emergency_exits: int = 0

    # Profit metrics
    total_gross_profit: float = 0.0
    total_fees_paid: float = 0.0
    total_net_profit: float = 0.0

    # Position metrics
    positions_merged: int = 0
    positions_resolved: int = 0

    # Timing metrics
    avg_fill_time: float = 0.0
    avg_merge_time: float = 0.0

    def to_summary(self) -> str:
        """Generate human-readable summary."""
        win_rate = self.both_sides_filled / max(self.opportunities_executed, 1) * 100
        profit_per_trade = self.total_net_profit / max(self.both_sides_filled, 1)

        return (
            f"📊 Gabagool Performance\n"
            f"Opportunities: {self.opportunities_detected} detected, {self.opportunities_executed} executed\n"
            f"Fill rate: {win_rate:.1f}%\n"
            f"Partial fills: {self.partial_fills}\n"
            f"Net profit: ${self.total_net_profit:.2f}\n"
            f"Profit/trade: ${profit_per_trade:.2f}"
        )
```

---

## References

### Implementation Sources

- [Inside the Mind of a Polymarket BOT](https://coinsbench.com/inside-the-mind-of-a-polymarket-bot-3184e9481f0a) - Gabagool strategy breakdown
- [Building a Prediction Market Arbitrage Bot](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage) - Technical architecture
- [How to Programmatically Identify Arbitrage Opportunities](https://medium.com/@wanguolin/how-to-programmatically-identify-arbitrage-opportunities-on-polymarket-and-why-i-built-a-portfolio-23d803d6a74b) - Detection algorithms
- [Polymarket HFT: How Traders Use AI](https://www.quantvps.com/blog/polymarket-hft-traders-use-ai-arbitrage-mispricing) - Execution strategies

### Official Documentation

- [Polymarket Trading Fees](https://docs.polymarket.com/polymarket-learn/trading/fees) - **Authoritative fee structure** (NO winner fee!)
- [Polymarket Maker Rebates Program](https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program)
- [Polymarket Merging Tokens](https://docs.polymarket.com/developers/CTF/merge)
- [py-clob-client GitHub](https://github.com/Polymarket/py-clob-client)

### Academic Research

- [Application of the Kelly Criterion to Prediction Markets](https://arxiv.org/abs/2412.14144) - Position sizing
- [Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets](https://arxiv.org/abs/2508.03474) - Academic analysis of arbitrage

### Fee Structure Clarification

**IMPORTANT**: Many third-party sources incorrectly claim a "2% winner fee" on Polymarket.
According to official Polymarket documentation:
- Most markets are **fee-free**
- 15-minute crypto markets have **taker fees only** (up to 1.56% at 50%)
- These taker fees fund the **Maker Rebates Program**
- There is **NO fee on profits/winnings**

This significantly improves Gabagool profitability compared to calculations that assume a 2% fee.

---

*Plan created: January 2026*
*Plan updated: January 2026 - Corrected fee structure (no 2% winner fee)*
*For: poly-maker project*
