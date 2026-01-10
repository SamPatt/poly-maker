# Market Making Research: Ensuring Both Sides Get Filled

## The Problem

In our 15-minute crypto rebates bot, we observed that **22% of markets had partial fills** (only one side filled). This exposes us to directional risk instead of the intended delta-neutral position.

**Observed pattern from logs:**
```
UP: 0.48 -> 0.47 -> 0.45 -> 0.44  (prices dropping in bearish market)
DOWN: 0.49 -> 0.50 -> 0.51 -> 0.52  (prices rising in bearish market)
```

When the market is directional:
- **Bullish**: Everyone buys UP, our DOWN order doesn't fill
- **Bearish**: Everyone buys DOWN, our UP order doesn't fill

---

## Research Findings

### Adverse Selection

From [Crypto Chassis on Medium](https://medium.com/open-crypto-market-data-initiative/defensive-market-making-against-market-manipulators-3ceabb5d1b71):

> "When markets make large trending movements, simple market makers are very susceptible to something called 'adverse selection' and can quickly become unfortunate victims due to attacks by sophisticated traders (who are called 'informed traders')."

> "Through backtest experiments, adverse selection can drain accounts at astonishingly fast speed."

### Inventory Risk and One-Sided Flow

From [HeLa Labs](https://helalabs.com/blog/market-maker-in-crypto/):

> "One particular risk for crypto market makers is inventory imbalance. They strive to skew bid and ask quotes to drive trades that rebalance inventory to neutral levels."

> "Periods of volatility often come with one-sided order flow — think panic selling or euphoric buying. In response, market makers adjust their quotes asymmetrically, nudging bid and ask prices away from the pressure side to reduce exposure."

### Professional Market Maker Techniques

From [Rootstone](https://rootstone.io/insights/what-is-inventory-risk-and-how-market-makers-manage-it):

1. **Hedging**: Offset inventory exposure with correlated assets or derivatives
2. **Dynamic Quoting**: Algorithms skew quotes based on volatility and order flow
3. **Risk Limits**: Strict inventory limits with automated quote withdrawal
4. **Diversification**: Spread inventory across uncorrelated assets
5. **Advanced Analytics**: Tools like VPIN for real-time adjustments

### Avellaneda-Stoikov Strategy

From [MadeinArk](https://madeinark.org/automated-market-making-bots-in-cryptocurrency-from-spread-capture-to-advanced-inventory-management/):

> "The Avellaneda-Stoikov strategy teaches market makers to adjust bid/ask prices to hedge against inventory risk, with the adjustment linearly proportional to the one-side inventory excess."

### Polymarket-Specific Insights

From [Polymarket's blog](https://news.polymarket.com/p/automated-market-making-on-polymarket):

> "Low volatility means you're less likely to get stuck with a bad position when news hits. You can place orders on both sides and sleep well knowing you won't wake up to massive losses."

The [Polymarket poly-market-maker](https://github.com/Polymarket/poly-market-maker) uses a "bands strategy" that places orders around the **midpoint price**, not chasing the best ask.

---

## Root Cause Analysis

### Our Current Approach (Flawed)

```python
# We price based on the ASK (sell orders)
our_price = best_ask - 2 * tick_size
```

**Problem**: We're placing BUY orders (bids), but pricing based on the ASK side. We should be considering our competition - other BUY orders (bids).

### How Order Matching Works

When we place a **BUY UP at 0.48**:
- We're putting a bid on the order book
- For it to fill, a seller needs to hit our bid
- Sellers hit the **highest bids first** (price-time priority)
- If other buyers are bidding 0.49 or 0.50, they get filled before us

### Example: Bearish Market

**DOWN order book:**
- Best bid: 0.50 (other buyers)
- Best ask: 0.52 (sellers)
- We place at: best_ask - 2 ticks = **0.50**
- Result: We're in a queue with other 0.50 buyers, might not fill

**UP order book:**
- Best bid: 0.44 (other buyers)
- Best ask: 0.47 (sellers)
- We place at: best_ask - 2 ticks = **0.45**
- Result: We're **above** the best bid, we're competitive, we fill!

This explains why we get partial fills - we're competitive on one side but not the other.

---

## Potential Solutions

### 1. Bid-Competitive Pricing (Recommended)

Price based on best BID (our competition), not just best ASK:

```python
# Current (flawed):
our_price = best_ask - 2 * tick_size

# Better:
our_price = max(best_bid + tick_size, best_ask - 2 * tick_size)
# Be above other buyers, but don't cross the book
```

**Pros**: Directly addresses the competition problem
**Cons**: May result in worse execution prices

### 2. Symmetric Pricing

Force both sides to the same price:

```python
up_price = get_best_maker_price(up_token)   # 0.44
down_price = get_best_maker_price(down_token) # 0.52
avg_price = (up_price + down_price) / 2      # 0.48
# Use 0.48 for both
```

**Pros**: Simple, ensures equal competitiveness
**Cons**: Doesn't guarantee we're competitive on either side

### 3. Constrained Divergence

Allow prices to adapt but cap the maximum spread:

```python
MAX_DIVERGENCE = 0.02  # 2 ticks

if abs(up_price - down_price) > MAX_DIVERGENCE:
    mid = (up_price + down_price) / 2
    up_price = mid
    down_price = mid
```

**Pros**: Balances adaptation with symmetry
**Cons**: May still miss fills in fast markets

### 4. Quote at Midpoint

Industry standard approach - quote at the midpoint between bid and ask:

```python
midpoint = (best_bid + best_ask) / 2
our_price = midpoint - tick_size  # Slightly below mid for safety
```

**Pros**: Standard market maker approach
**Cons**: May be too conservative in thin books

### 5. Accept Partial Fills

78% full fill rate might be acceptable. Economics still work:
- **Both fill**: Earn rebates, zero directional risk
- **One fills**: Earn rebates on filled side, 15-min directional exposure

**Pros**: Simplest, no code changes
**Cons**: Some directional risk, lower capital efficiency

---

## Implementation Priority

1. **Short term**: Implement bid-competitive pricing (Solution 1)
2. **Monitor**: Track fill rates after change
3. **Iterate**: If still problematic, add constrained divergence (Solution 3)

---

## References

- [Automated Market Making on Polymarket](https://news.polymarket.com/p/automated-market-making-on-polymarket)
- [Polymarket poly-market-maker GitHub](https://github.com/Polymarket/poly-market-maker)
- [Defensive Market Making Against Manipulators](https://medium.com/open-crypto-market-data-initiative/defensive-market-making-against-market-manipulators-3ceabb5d1b71)
- [Market Making Mechanics and Strategies](https://medium.com/blockapex/market-making-mechanics-and-strategies-4daf2122121c)
- [Market Maker in Crypto: Strategies and Inventory Control](https://helalabs.com/blog/market-maker-in-crypto/)
- [4 Core Crypto Market Making Strategies - DWF Labs](https://www.dwf-labs.com/news/4-common-strategies-that-crypto-market-makers-use)
- [What Is Inventory Risk - Rootstone](https://rootstone.io/insights/what-is-inventory-risk-and-how-market-makers-manage-it)
- [Delta Neutral Market Making Strategy - Autowhale](https://www.autowhale.io/post/developing-a-delta-neutral-market-making-strategy)
