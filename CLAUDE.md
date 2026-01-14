# Development Workflow

## Environment Setup

This project has two environments:

1. **Local Development Machine** - Where code is written, tested, and committed
2. **VPS (trading server)** - Where the bot runs in production

## Development Rules

### All Development Happens Locally

**IMPORTANT: All code changes must be made locally, not on the VPS.**

The proper workflow is:

1. Make changes in your local repository
2. Run tests locally: `pytest tests/unit/active_quoting/ -v`
3. Commit and push to GitHub
4. SSH into the VPS and pull the changes

```bash
# On VPS
cd /home/polymaker/poly-maker
git pull origin main
```

### Never Edit Code Directly on VPS

The VPS should only be used for:
- Running the bot in production
- Checking logs
- Monitoring status
- Pulling updates from GitHub

Do NOT:
- Edit code files directly on the VPS
- Make commits on the VPS
- Run development tasks on the VPS

### Exception: Emergency Hotfixes

In rare emergency situations where a critical bug needs immediate fixing:

1. Create a new branch on VPS: `git checkout -b hotfix-description`
2. Make minimal changes
3. Commit on VPS
4. Push to GitHub
5. Pull to local machine and merge properly
6. Pull merged changes back to VPS main branch

## Accessing the VPS

```bash
ssh trading
cd /home/polymaker/poly-maker
```

## Bot Code Location

- Main code: `/home/polymaker/poly-maker/rebates/active_quoting/`
- Tests: `/home/polymaker/poly-maker/tests/unit/active_quoting/`
- Data files: `/home/polymaker/poly-maker/data/`

## Running Tests

```bash
# From project root
./.venv/bin/pytest tests/unit/active_quoting/ -v

# Run specific test file
./.venv/bin/pytest tests/unit/active_quoting/test_bot.py -v
```

## Key Configuration

Environment variables for the AQ bot are documented in `rebates/active_quoting/config.py`.

Key variables:
- `AQ_DRY_RUN` - Set to "true" for testing without real orders
- `AQ_ORDER_SIZE_USDC` - Size of orders in USDC
- `AQ_MAX_POSITION_PER_MARKET` - Maximum position size per market
