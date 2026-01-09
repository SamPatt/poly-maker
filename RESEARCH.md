# Polymarket Market Making Research

This document summarizes research on market making on Polymarket, compiled from official documentation, community sources, and analysis of the poly-maker codebase.

## Overview

Market making on Polymarket involves providing liquidity by continuously posting bid and ask orders on prediction markets. Revenue comes from:
1. **Bid-ask spread** - Profit from the difference between buy and sell prices
2. **Liquidity rewards** - Polymarket's incentive program for two-sided liquidity
3. **Maker rebates** - Fee reductions for market-making activities

## Polymarket Technical Infrastructure

### CLOB (Central Limit Order Book)
- **Hybrid-decentralized system**: Off-chain matching, on-chain settlement
- **EIP712-signed orders**: Orders are structured as signed messages
- **Atomic swaps**: Exchange contract executes non-custodial swaps between outcome tokens and USDC
- **Unified orderbook**: Complementary tokens (YES/NO) share one orderbook

### Current Fee Structure
- **Maker**: 0 basis points (free)
- **Taker**: 0 basis points (free)
- Fees, when applicable, calculated as: `baseRate × min(price, 1-price) × size`

### Order Types
- **GTC (Good Till Cancelled)**: Standard passive quotes
- **GTD (Good Till Date)**: Auto-expire at specified time
- **FOK (Fill or Kill)**: Require complete immediate fill
- **FAK (Fill and Kill)**: Partial fills accepted, remainder cancelled

### Tick Sizes
Markets have specific tick sizes: "0.1", "0.01", "0.001", or "0.0001". Orders must conform to market tick size.

## Profitability Insights

### From Experienced Operators
One operator reported (2024-2025):
- Started with $10,000 capital
- Earned ~$200/day initially
- Scaled to $700-800/day at peak
- Primary revenue: Polymarket's liquidity rewards program

Key insight: "The reward system isn't perfectly calibrated to risk. Some markets barely move but offer huge rewards relative to their volatility."

### Market Selection Strategy
The codebase implements a volatility-based market selection:
- Calculate volatility across timeframes (1hr, 3hr, 6hr, 12hr, 24hr, 7day, 30day)
- Compute `volatility_sum` to rank markets
- Calculate `gm_reward_per_100` (reward per $100 invested)
- Target: Low volatility + high rewards

### Risk Factors
1. **Price volatility** - Sudden moves can cause losses
2. **Reward program changes** - Rates reportedly decreased post-2024 election
3. **News events** - Can dramatically shift market prices
4. **Capital lock-up** - Positions across multiple markets

## This Bot's Strategy

### Core Approach
1. **Two-sided quotes**: Maintain both bid and ask orders
2. **Position limits**: Configurable `max_size` per market
3. **Volatility filtering**: Skip high-volatility markets (`3_hour > volatility_threshold`)
4. **Spread management**: Only place orders within incentive spread range

### Risk Management
- **Stop-loss**: Triggers when PnL < threshold AND spread < threshold
- **Risk-off periods**: After stop-loss, pause trading for `sleep_period` hours
- **Position merging**: Recover USDC when holding both YES and NO
- **Price boundaries**: Only trade 0.1-0.9 range (avoid extremes)

### Order Sizing
- `trade_size`: Amount to quote per order
- `max_size`: Maximum position per token (absolute cap: 250)
- `min_size`: Minimum order size (avoid dust)
- `multiplier`: Scaling factor for low-priced assets (<0.1)

## Getting Started Requirements

### From Polymarket Docs
1. Email support@polymarket.com for RFQ API access
2. Deploy wallets, fund with USDCe
3. Configure token approvals
4. Integrate WebSocket for orderbook monitoring
5. Post orders via CLOB REST API

### From This Codebase
1. Polygon wallet with at least one UI trade completed
2. Google Service Account for Sheets integration
3. Copy sample spreadsheet and configure markets
4. Run `update_markets.py` continuously (separate IP recommended)
5. Run `main.py` for trading

## Areas of Uncertainty / Further Investigation

### Technical Questions
- [ ] What are current liquidity reward rates? (may have changed)
- [ ] How does the RFQ (Request for Quote) API differ from standard CLOB?
- [ ] What are rate limits on the CLOB API?
- [ ] Is there geographic restriction (US-only for web search noted)?

### Strategic Questions
- [ ] Optimal capital allocation across markets?
- [ ] How to handle market resolution timing?
- [ ] Best practices for scaling up capital?
- [ ] How competitive is the current market making landscape?

### Codebase Gaps
- [ ] No automated tests in the repository
- [ ] Hardcoded values in trading logic (e.g., `250` absolute cap, `0.05` price change threshold)
- [ ] Error handling could be more robust
- [ ] No logging/monitoring infrastructure

## Resources

### Official Documentation
- [Market Makers Introduction](https://docs.polymarket.com/developers/market-makers/introduction)
- [CLOB Introduction](https://docs.polymarket.com/developers/CLOB/introduction)
- [Trading Guide](https://docs.polymarket.com/developers/market-makers/trading)

### Smart Contracts
- [Exchange Contract Source](https://github.com/Polymarket/ctf-exchange/tree/main/src)
- [Exchange Contract Docs](https://github.com/Polymarket/ctf-exchange/blob/main/docs/Overview.md)

### Community
- Sample Google Sheet: https://docs.google.com/spreadsheets/d/1Kt6yGY7CZpB75cLJJAdWo7LSp9Oz7pjqfuVWwgtn7Ns/

## Path to $300/Day Goal

Based on research, a reasonable progression:

1. **Start small** ($500-1000): Validate setup works, understand market dynamics
2. **Identify opportunities**: Use `update_markets.py` to find low-volatility, high-reward markets
3. **Tune parameters**: Adjust hyperparameters based on results
4. **Scale capital**: Increase position sizes as confidence grows
5. **Diversify markets**: Spread across multiple markets to reduce correlation risk

The operator who earned $700-800/day started with $10k. Proportionally, $300/day may require $4-5k+ in deployed capital, depending on current reward rates.
