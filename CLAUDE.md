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
uv run python main.py             # Run the market making bot
uv run python update_markets.py   # Update market data (run continuously, separate IP recommended)
uv run python update_stats.py     # Update account statistics
```

### Development
```bash
uv run black .                    # Format code (line-length=100)
uv run pytest                     # Run tests
```

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
- Position merging when holding both YES/NO
- Order placement with spread/incentive calculations
- Stop-loss triggers based on PnL and volatility
- Take-profit sell order management
- Risk-off periods after stop-loss

**`poly_data/`**
- `global_state.py` - Shared mutable state (positions, orders, market data, params)
- `polymarket_client.py` - Wrapper around `py-clob-client` with blockchain interactions
- `websocket_handlers.py` - Market and user WebSocket connections
- `data_processing.py` - Processes WebSocket events, triggers trades
- `data_utils.py` - Position/order state management, Google Sheets sync
- `trading_utils.py` - Price calculations, order sizing logic

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
