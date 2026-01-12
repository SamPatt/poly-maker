# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Poly-Maker is an automated market making bot for Polymarket prediction markets. It provides liquidity by maintaining two-sided orders (bids and asks) on selected markets, earning from bid-ask spreads and Polymarket's liquidity rewards program.

## Commands

### Setup
```bash
uv sync                           # Install Python dependencies
uv sync --extra dev               # Install with dev dependencies (black, pytest)
cd poly_merger && npm install     # Install Node.js dependencies for position merging
cp .env.example .env              # Create environment file
```

### Running
```bash
uv run python main.py                  # Run the market making bot
uv run python update_markets.py        # Update market data (run continuously, separate IP recommended)
uv run python update_stats.py          # Update account statistics
uv run python -m rebates.rebates_bot   # Run 15-minute rebates bot (separate process)
uv run python -m rebates.gabagool.run  # Run Gabagool arbitrage bot (separate process)
```

### Development
```bash
uv run black .                    # Format code (line-length=100)
uv run pytest                     # Run tests
```

### Testing
```bash
# Run all unit tests
uv run pytest tests/unit/ -v

# Run specific test file
uv run pytest tests/unit/test_trading_utils.py -v

# Run with coverage report
uv run pytest tests/unit/ --cov=poly_data --cov=trading --cov-report=term-missing

# Run integration tests (requires credentials and POLY_TEST_INTEGRATION=true)
POLY_TEST_INTEGRATION=true uv run pytest tests/integration/ -v

# Run all tests with short traceback
uv run pytest tests/ -v --tb=short
```

## Testing Infrastructure

### Test Directory Structure
```
tests/
├── conftest.py                        # Shared fixtures (mock_global_state, mock_client)
├── unit/
│   ├── test_trading_utils.py          # Orderbook analysis, price calculations
│   ├── test_data_utils.py             # Position/order state management
│   ├── test_trading.py                # Order creation, cancellation logic
│   ├── test_telegram.py               # Alert formatting
│   ├── test_circuit_breaker.py        # Gabagool circuit breaker (26 tests)
│   ├── test_gabagool_scanner.py       # Gabagool orderbook scanning (16 tests)
│   ├── test_gabagool_monitor.py       # Gabagool detection loop (16 tests)
│   ├── test_gabagool_executor.py      # Gabagool order execution (21 tests)
│   ├── test_gabagool_reconciler.py    # Gabagool partial fill handling (10 tests)
│   └── test_gabagool_position_manager.py  # Position lifecycle (28 tests)
├── integration/
│   ├── test_polymarket_client.py      # Real API tests (read-only)
│   └── test_database.py               # Database connectivity tests
└── fixtures/
    └── market_data.py                 # Sample orderbooks, market rows, positions
```

### Key Test Fixtures
- `sample_orderbook` - Realistic bid/ask data with SortedDicts
- `sample_market_row` - Market configuration with all required fields
- `mock_global_state` - Patched global_state with controlled data
- `mock_client` - Mocked PolymarketClient with predictable responses

### Observability Modules
- `poly_data/exceptions.py` - Custom exception hierarchy with alert classification
- `poly_data/logging_config.py` - Structured JSON logging with context support
- `poly_data/retry.py` - Retry decorators with exponential backoff

### Enhanced Telegram Alerts
New alert functions in `alerts/telegram.py`:
- `send_critical_error_alert()` - For exceptions with should_alert=True
- `send_websocket_reconnect_alert()` - After 3+ reconnection attempts
- `send_balance_warning_alert()` - Low balance warnings
- `send_market_exit_alert()` - Exit before event notifications
- `send_high_volatility_alert()` - Volatility threshold alerts

### Position Merging (standalone)
```bash
node poly_merger/merge.js [amount] [condition_id] [is_neg_risk]
# Example: node poly_merger/merge.js 1000000 0xabc123 true
```

## Architecture

### Data Flow
1. **Supabase/Google Sheets** → Configuration source for markets, hyperparameters (configurable via DATA_SOURCE env)
2. **WebSocket** → Real-time orderbook updates trigger trading decisions
3. **Trading Logic** → Analyzes spreads, positions, volatility to place/cancel orders
4. **CLOB API** → Order execution via `py-clob-client`
5. **Blockchain** → Position merging via Node.js subprocess
6. **Telegram** → Trade alerts and daily summaries (optional)

### Key Modules

**`main.py`** - Entry point. Initializes client, starts background update thread, maintains WebSocket connections.

**`trading.py`** - Core trading logic in `perform_trade()`:
- **Two-sided market making** - Places both BUY and ASK orders for liquidity rewards
- Position merging when holding both YES/NO
- Order placement with spread/incentive calculations
- Stop-loss triggers based on PnL and volatility (requires position)
- Take-profit sell order management (when holding inventory)
- Market-making ASK orders (even without inventory, for two-sided rewards)
- Risk-off periods after stop-loss

**`poly_data/`**
- `global_state.py` - Shared mutable state (positions, orders, market data, params)
- `polymarket_client.py` - Wrapper around `py-clob-client` with blockchain interactions
- `websocket_handlers.py` - Market and user WebSocket connections
- `data_processing.py` - Processes WebSocket events, triggers trades
- `data_utils.py` - Position/order state management, Google Sheets sync
- `trading_utils.py` - Price calculations, order sizing logic, two-sided quoting

**`poly_merger/`** - Node.js utility for on-chain position merging via Gnosis Safe

**`data_updater/`** - Separate module for fetching all available markets and calculating volatility metrics

**`db/`** - Supabase integration (alternative to Google Sheets)
- `supabase_client.py` - Database connection and queries
- `schema.sql` - Table definitions for Supabase setup

**`alerts/`** - Telegram notification system
- `telegram.py` - Trade alerts, errors, daily summaries

**`deploy/`** - Deployment configurations
- `trading.service` - systemd service for trading bot
- `updater.service` - systemd service for data updater
- `webui.service` - systemd service for web UI
- `setup.sh` - VPS setup script

**`web/`** - Web UI for configuration
- `app.py` - FastAPI application
- `templates/` - Jinja2 HTML templates

**`rebates/`** - 15-Minute Crypto Trading Strategies
- `rebates_bot.py` - Main entry point for rebates strategy
- `market_finder.py` - Discovers upcoming BTC/ETH/SOL 15-minute Up/Down markets from Gamma API
- `strategy.py` - Delta-neutral order placement (buys both Up and Down at 50%)
- `config.py` - Configuration (trade size, safety buffer, dry run mode)

**`rebates/gabagool/`** - Gabagool Arbitrage Strategy (YES+NO < $1.00)
- `monitor.py` - Main detection and execution loop
- `scanner.py` - Orderbook scanning with VWAP anti-spoofing
- `executor.py` - Order execution (taker/maker/hybrid strategies)
- `position_manager.py` - Position lifecycle management with persistence
- `reconciler.py` - Partial fill handling and rescue operations
- `circuit_breaker.py` - Risk management (position limits, loss limits, error tracking)
- `config.py` - Environment-based configuration
- `run.py` - CLI runner with dry-run support

## VPS Deployment

### SSH Access

SSH config is set up in `~/.ssh/config`:
```
Host trading
    HostName 77.42.39.205
    User root
    IdentityFile ~/.ssh/hetzner

Host updater
    HostName 46.224.186.58
    User root
    IdentityFile ~/.ssh/hetzner
```

Connect with: `ssh trading` or `ssh updater`

**Project directory on VPS:** `/home/polymaker/poly-maker`

### Initial Setup
```bash
# On VPS as root, run the setup script:
./deploy/setup.sh trading   # For VPS 1 (trading bot + PostgreSQL)
./deploy/setup.sh updater   # For VPS 2 (data updater only)
```

### Service Management

**Trading Bot (on trading server):**

The bot runs in a `screen` session so it persists after SSH disconnect.

```bash
# SSH into trading server
ssh trading

# Start bot in screen session (recommended)
screen -dmS trading bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && python -u main.py 2>&1 | tee /tmp/trading.log'

# Attach to see live output
screen -r trading
# Press Ctrl+A, then D to detach without stopping

# View logs without attaching
tail -f /tmp/trading.log

# Check if running
screen -ls              # Shows active screen sessions
pgrep -f 'python.*main.py'

# Stop the bot
screen -S trading -X quit

# Start manually (foreground, useful for debugging)
cd /home/polymaker/poly-maker
source .venv/bin/activate
python main.py
```

**Data Updater (on updater server):**

```bash
# SSH into updater server
ssh updater

# Start updater in screen session (recommended)
screen -dmS updater bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && python -u update_markets.py 2>&1 | tee /tmp/updater.log'

# Attach to see live output
screen -r updater
# Press Ctrl+A, then D to detach without stopping

# View logs without attaching
tail -f /tmp/updater.log

# Check if running
screen -ls
pgrep -f 'update_markets.py'

# Stop the updater
screen -S updater -X quit
```

**Using systemd (if services are installed):**
```bash
sudo systemctl start trading
sudo systemctl stop trading
sudo systemctl restart trading
sudo systemctl status trading
sudo journalctl -u trading -f
```

### Web UI Management

The web UI runs on port 8080, accessible only via Tailscale.

**Accessing the Web UI:**
```bash
# From your local machine (must be on the same Tailscale network)
http://trading:8080           # Using Tailscale hostname
http://100.84.112.124:8080    # Using Tailscale IP directly
```

**Starting the Web UI (on trading server):**
```bash
# SSH into trading server first
ssh trading

# Start in screen session (recommended - persists after SSH disconnect)
screen -dmS webui bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && uvicorn web.app:app --host 0.0.0.0 --port 8080 2>&1 | tee /tmp/webui.log'

# Attach to see live output
screen -r webui

# Start manually (foreground, useful for debugging)
cd /home/polymaker/poly-maker
source .venv/bin/activate
uvicorn web.app:app --host 0.0.0.0 --port 8080
```

**Stopping the Web UI:**
```bash
# Stop screen session
screen -S webui -X quit

# Or kill the process directly
pkill -f 'uvicorn.*app:app'
```

**Checking Web UI status:**
```bash
# Check if running
screen -ls                    # Shows webui screen session
pgrep -f 'uvicorn.*app:app'

# Test locally on server
curl http://localhost:8080/api/status

# View logs
tail -f /tmp/webui.log
```

### Remote Commands (from local machine)

Run commands on VPS without interactive SSH session:

```bash
# Pull latest code
ssh trading "cd /home/polymaker/poly-maker && git pull origin main"

# Restart trading bot
ssh trading "pkill -f 'python.*main.py' || true; screen -S trading -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS trading bash -c 'source .venv/bin/activate && python -u main.py 2>&1 | tee /tmp/trading.log'"

# Restart web UI
ssh trading "pkill -f 'uvicorn.*app:app' || true; screen -S webui -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS webui bash -c 'source .venv/bin/activate && uvicorn web.app:app --host 0.0.0.0 --port 8080 2>&1 | tee /tmp/webui.log'"

# Check bot status
ssh trading "pgrep -f 'python.*main.py' && echo 'Bot running' || echo 'Bot stopped'"

# View recent logs
ssh trading "tail -50 /tmp/trading.log"

# Check API status
ssh trading "curl -s http://localhost:8080/api/status"
```

**Note:** When running commands via SSH that start background processes, use `screen -dmS` rather than `nohup`. The `nohup` approach can cause SSH exit code 255 issues.

### 15-Minute Rebates Bot

The rebates bot runs separately from the main trading bot to capture maker rebates on Polymarket's 15-minute crypto Up/Down markets.

**Strategy:**
- Finds upcoming BTC/ETH/SOL 15-minute markets
- Places delta-neutral orders (both Up and Down at 50% price)
- Earns maker rebates when takers fill orders (up to 1.56% at 50%)
- No directional risk - profits regardless of market outcome

**CRITICAL:** Only trades on UPCOMING markets (before eventStartTime), never on LIVE markets.

**Running the Rebates Bot:**
```bash
# SSH into trading server
ssh trading

# Start in dry-run mode (default - simulates trades)
screen -dmS rebates bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'

# Start with LIVE trading enabled
screen -dmS rebates bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && REBATES_DRY_RUN=false python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'

# Attach to see live output
screen -r rebates

# View logs
tail -f /tmp/rebates.log

# Stop the bot
screen -S rebates -X quit
```

**Remote commands (from local machine):**
```bash
# Start rebates bot (dry-run)
ssh trading "screen -S rebates -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"

# Start rebates bot (LIVE trading)
ssh trading "screen -S rebates -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && REBATES_DRY_RUN=false python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"

# Check status
ssh trading "pgrep -f 'rebates.rebates_bot' && echo 'Rebates bot running' || echo 'Rebates bot stopped'"

# View logs
ssh trading "tail -30 /tmp/rebates.log"
```

**Configuration (via environment variables):**
- `REBATES_DRY_RUN=true` - Simulate trades without executing (default: true)
- `REBATES_TRADE_SIZE=5` - USDC per side (default: 5, Polymarket minimum)
- `REBATES_SAFETY_BUFFER=30` - Seconds before market start to stop trading (default: 30)
- `REBATES_CHECK_INTERVAL=60` - Seconds between market scans (default: 60)
- `REBATES_MAX_IMBALANCE=10` - Max allowed position imbalance in shares (default: 10)

**Position Imbalance Protection:**

The rebates bot places maker orders on both Up and Down sides, but takers may fill one side more frequently than the other. This creates directional risk (e.g., holding 31 Up vs 15 Down). The goal is always to match quantities - any imbalance is risk.

**Three layers of protection:**

| Mechanism | Threshold | Purpose |
|-----------|-----------|---------|
| **Prevention** | > MAX_IMBALANCE (10) | Skip overweight side on NEW markets |
| **Rebalance** | >= 1 share | Always try to match on LIVE markets |
| **Rescue** | Per-market | Fill unfilled side within same market |

1. **Prevention** (new markets): If imbalance exceeds `MAX_POSITION_IMBALANCE`, skip placing orders on the overweight side for new markets. This prevents making imbalance worse.

2. **Rebalance** (cross-market): On ANY imbalance >= 1 share, actively place orders on the underweight side across all LIVE markets. Markets swing through 50% during trading, so maker orders at ~50% (capped at 52%) can fill.

3. **Rescue** (per-market): When one side of a specific market fills but the other doesn't, aggressively try to fill the unfilled side with maker orders (or taker as last resort in final 2 minutes).

**Log messages:**
- Prevention: `IMBALANCE: Up=66.3 Down=50.0 (imbalance=+16.3) - skipping Up order to rebalance`
- Rebalance: `REBALANCE DOWN: Placing order @ 0.50 on Bitcoin Up or Down... (imbalance=+16.3)`
- Rescue: `RESCUE DOWN: Aggressive maker 0.49 -> 0.50 (80% agg, LIVE, 540s to resolution)`
- Status: `Positions: Up=66.3 Down=50.0 Imbalance=+16.3 [REBALANCING]`

**Testing:**
```bash
# Dry run (default) - watch logs for market discovery
REBATES_DRY_RUN=true python -m rebates.rebates_bot

# Minimum capital test (Polymarket minimum is 5)
REBATES_DRY_RUN=false REBATES_TRADE_SIZE=5 python -m rebates.rebates_bot

# Full operation
REBATES_DRY_RUN=false REBATES_TRADE_SIZE=10 python -m rebates.rebates_bot
```

### Gabagool Arbitrage Bot

The Gabagool bot exploits arbitrage opportunities when YES + NO prices on 15-minute crypto markets sum to less than $1.00. Buying both sides guarantees profit at settlement since one side always pays $1.00.

**Strategy:**
- Scans orderbooks for opportunities where combined YES + NO cost < $0.99
- Uses VWAP pricing to avoid spoofing attacks from small orders
- Executes paired orders simultaneously to minimize directional exposure
- Merges balanced positions immediately to realize profit
- Handles partial fills with automatic rescue operations

**Key Features:**
- Three execution strategies: taker (fast), maker (cheap), hybrid (recommended)
- Circuit breaker for risk management (position limits, loss limits, error tracking)
- Position persistence across restarts
- Automatic post-resolution redemption

**Running the Gabagool Bot:**
```bash
# SSH into trading server
ssh trading

# Start in dry-run mode (default - simulates trades)
screen -dmS gabagool bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && python -u -m rebates.gabagool.run 2>&1 | tee /tmp/gabagool.log'

# Start with LIVE trading enabled
screen -dmS gabagool bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && GABAGOOL_DRY_RUN=false python -u -m rebates.gabagool.run 2>&1 | tee /tmp/gabagool.log'

# Detection only (no execution)
screen -dmS gabagool bash -c 'cd /home/polymaker/poly-maker && source .venv/bin/activate && python -u -m rebates.gabagool.run --detect-only 2>&1 | tee /tmp/gabagool.log'

# Attach to see live output
screen -r gabagool

# View logs
tail -f /tmp/gabagool.log

# Stop the bot
screen -S gabagool -X quit
```

**Remote commands (from local machine):**
```bash
# Start gabagool bot (dry-run)
ssh trading "screen -S gabagool -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS gabagool bash -c 'source .venv/bin/activate && python -u -m rebates.gabagool.run 2>&1 | tee /tmp/gabagool.log'"

# Start gabagool bot (LIVE trading)
ssh trading "screen -S gabagool -X quit 2>/dev/null || true"
ssh trading "cd /home/polymaker/poly-maker && screen -dmS gabagool bash -c 'source .venv/bin/activate && GABAGOOL_DRY_RUN=false python -u -m rebates.gabagool.run 2>&1 | tee /tmp/gabagool.log'"

# Check status
ssh trading "pgrep -f 'rebates.gabagool.run' && echo 'Gabagool bot running' || echo 'Gabagool bot stopped'"

# View logs
ssh trading "tail -30 /tmp/gabagool.log"
```

**Configuration (via environment variables):**
- `GABAGOOL_DRY_RUN=true` - Simulate trades without executing (default: true)
- `GABAGOOL_PROFIT_THRESHOLD=0.99` - Max combined YES+NO cost to trade (default: 0.99)
- `GABAGOOL_TRADE_SIZE=50` - Position size per opportunity in USDC (default: 50)
- `GABAGOOL_SCAN_INTERVAL=1.0` - Seconds between scans (default: 1.0)
- `GABAGOOL_MIN_LIQUIDITY=50` - Minimum shares available (default: 50)
- `GABAGOOL_MIN_NET_PROFIT=0.5` - Minimum net profit % to execute (default: 0.5)

**Circuit Breaker Configuration:**
- `GABAGOOL_CB_MAX_POS_MARKET=500` - Max position per market
- `GABAGOOL_CB_MAX_POS_TOTAL=2000` - Max total position across all markets
- `GABAGOOL_CB_MAX_DAILY_LOSS=100` - Max daily loss before halt
- `GABAGOOL_CB_MAX_CONSEC_ERRORS=5` - Max consecutive errors before halt
- `GABAGOOL_CB_COOLDOWN=300` - Cooldown seconds after halt

**Testing:**
```bash
# Quick one-shot scan to see current spreads
python -m rebates.gabagool.scan_once

# Run in dry-run mode
python -m rebates.gabagool.run

# Run detection only (no execution even in live mode)
python -m rebates.gabagool.run --detect-only

# Run with verbose logging
python -m rebates.gabagool.run -v

# Run Gabagool unit tests (117 tests)
uv run pytest tests/unit/test_gabagool*.py tests/unit/test_circuit_breaker.py -v
```

### State Management

Global state in `poly_data/global_state.py` tracks:
- `df` - Market configuration DataFrame from Google Sheets
- `params` - Hyperparameters by market type (stop_loss_threshold, take_profit_threshold, volatility_threshold, etc.)
- `positions` - Current token positions {token_id: {size, avgPrice}}
- `orders` - Open orders {token_id: {buy: {price, size}, sell: {price, size}}}
- `all_data` - Real-time orderbook data from WebSocket
- `performing` - Trade IDs currently being processed (prevents duplicate trades)
- `REVERSE_TOKENS` - Maps YES↔NO token pairs

### Trading Parameters (from Google Sheets)

Key hyperparameters per market type:
- `stop_loss_threshold` - PnL % to trigger stop-loss
- `take_profit_threshold` - Profit % target for sells
- `volatility_threshold` - Max volatility to accept trades
- `spread_threshold` - Spread required for stop-loss execution
- `sleep_period` - Hours to pause trading after stop-loss

Per-market settings:
- `trade_size`, `max_size`, `min_size` - Position sizing
- `max_spread` - Maximum spread for incentive calculations
- `tick_size` - Price precision (0.01, 0.001, etc.)
- `neg_risk` - Whether market uses negative risk adapter

### Polymarket Liquidity Rewards

The bot is designed to earn Polymarket's liquidity rewards by quoting two-sided liquidity.

**Key Requirements for Rewards:**
- **Two-sided quoting**: Must have both BID and ASK orders to maximize rewards
- **Minimum order size**: Polymarket minimum is 5 shares; each market has its own `min_incentive_size`
- **Spread from midpoint**: Tighter spreads score higher (quadratic scoring)
- **Extreme midpoints**: Markets with mid < 0.10 or > 0.90 REQUIRE two-sided liquidity (single-sided scores zero)

**How the Bot Achieves Two-Sided Quoting:**

1. `get_buy_sell_amount()` in `trading_utils.py` always returns both buy and sell amounts
2. Order size = `max(trade_size, min_incentive_size)` to meet reward thresholds
3. ASK orders are placed even without inventory (market-making mode)
4. With inventory, ASK orders use take-profit pricing; without inventory, competitive ask pricing

**Reward Scoring Formula:**
```
S(v,s) = ((v-s)/v)² × b

Where:
- v = max_spread from midpoint (e.g., 3.5 cents)
- s = your spread from midpoint
- b = in-game multiplier

Qmin = min(Qone, Qtwo)  # For extreme midpoints
Qmin = max(min(Qone, Qtwo), max(Qone/3, Qtwo/3))  # For normal range [0.10, 0.90]
```

**Configuration for Rewards:**
```python
# In hyperparameters (database):
max_size: 250      # Maximum position per outcome (must be >= min_incentive_size)
trade_size: 50     # Order size (will be increased to min_incentive_size if needed)
min_size: 5        # Polymarket minimum is 5

# Per-market (from all_markets table):
min_size: 50-200   # Market's min_incentive_size requirement
max_spread: 3.5    # Maximum spread from midpoint for incentives (cents)
```

**Warning:** If `min_incentive_size > max_size`, the bot will log a warning and orders may not qualify for rewards.

### Important Patterns

**Negative Risk Markets**: Some Polymarket markets use a "negative risk" structure requiring different contract calls. Check `row['neg_risk'] == 'TRUE'` before order creation and position merging.

**Position Merging**: When holding both YES and NO positions in same market, merging recovers USDC. Triggered when `min(pos_YES, pos_NO) > MIN_MERGE_SIZE` (20). Uses Node.js subprocess to interact with Polygon smart contracts.

**Risk-Off Periods**: After stop-loss triggers, bot writes to `positions/{market}.json` with `sleep_till` timestamp. New buys are blocked until this period expires.

**WebSocket Reconnection**: Main loop in `main.py` automatically reconnects when WebSocket connections drop.

## Environment Variables

Key settings in `.env`:
- `PK` - Polymarket private key
- `BROWSER_ADDRESS` - Wallet address
- `DATA_SOURCE` - `postgres` or `sheets` (default: sheets)
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` - PostgreSQL connection
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - Telegram alerts (optional)
- `DRY_RUN` - Set to `true` to simulate trades without executing

15-Minute Rebates Bot settings:
- `REBATES_DRY_RUN` - Set to `true` to simulate rebates trades (default: true)
- `REBATES_TRADE_SIZE` - USDC per side for rebates bot (default: 5, Polymarket minimum)
- `REBATES_SAFETY_BUFFER` - Seconds before market start to stop trading (default: 30)
- `REBATES_CHECK_INTERVAL` - Seconds between market scans (default: 60)

Gabagool Arbitrage Bot settings:
- `GABAGOOL_DRY_RUN` - Set to `true` to simulate trades (default: true)
- `GABAGOOL_PROFIT_THRESHOLD` - Max combined YES+NO to trade (default: 0.99)
- `GABAGOOL_TRADE_SIZE` - USDC per opportunity (default: 50)
- `GABAGOOL_SCAN_INTERVAL` - Seconds between scans (default: 1.0)
- `GABAGOOL_MIN_LIQUIDITY` - Minimum shares available (default: 50)
- `GABAGOOL_CB_MAX_DAILY_LOSS` - Circuit breaker daily loss limit (default: 100)

## External Dependencies

- **Polymarket API** (CLOB): `py-clob-client` for order management
- **Polygon RPC**: `https://polygon-rpc.com` for blockchain queries
- **PostgreSQL** (local) or **Google Sheets API**: Configuration and market selection
- **Gnosis Safe**: Wallet infrastructure for position merging
- **Telegram Bot API**: Trade notifications (optional)

## Contract Addresses (Polygon)

- Negative Risk Adapter: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- Conditional Tokens: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- USDC: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
