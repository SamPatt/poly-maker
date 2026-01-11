---
description: Run pytest tests with various options
argument-hint: [unit|integration|coverage|all|<test-file>]
allowed-tools: Bash
---

Run the test suite for poly-maker.

## Usage Options

Based on $ARGUMENTS:

- **No arguments or "all"**: Run all unit tests
  ```bash
  uv run pytest tests/unit/ -v --tb=short
  ```

- **"unit"**: Run only unit tests
  ```bash
  uv run pytest tests/unit/ -v --tb=short
  ```

- **"integration"**: Run integration tests (requires credentials)
  ```bash
  POLY_TEST_INTEGRATION=true uv run pytest tests/integration/ -v --tb=short
  ```

- **"coverage"**: Run unit tests with coverage report
  ```bash
  uv run pytest tests/unit/ --cov=poly_data --cov=trading --cov=alerts --cov-report=term-missing
  ```

- **Specific file path**: Run tests in that file
  ```bash
  uv run pytest $ARGUMENTS -v --tb=short
  ```

## Test Categories

- `tests/unit/test_trading_utils.py` - Orderbook analysis, price calculations (26 tests)
- `tests/unit/test_data_utils.py` - Position/order state management (21 tests)
- `tests/unit/test_trading.py` - Order creation, cancellation, DRY_RUN mode (22 tests)
- `tests/unit/test_telegram.py` - Alert formatting and disabled mode (13 tests)
- `tests/integration/` - Real API and database tests (require POLY_TEST_INTEGRATION=true)

## After Running

Report:
- Total tests passed/failed/skipped
- Any failing test names with brief error description
- Coverage percentage (if coverage option used)
