---
description: Stop the main trading bot on VPS
allowed-tools: Bash
---

Stop the main trading bot on the VPS with graceful shutdown.

The bot handles SIGHUP from screen quit, allowing it to send Telegram shutdown alert.

Execute:
1. Stop the trading bot screen session (sends SIGHUP for graceful shutdown):
   `ssh trading "screen -S trading -X quit 2>/dev/null || true"`

2. Wait for graceful shutdown:
   `sleep 3`

3. Verify it stopped:
   `ssh trading "pgrep -f 'python.*main.py' && echo 'Warning: Process still running' || echo 'Trading bot stopped'"`

4. If still running, force kill:
   `ssh trading "pkill -9 -f 'python.*main.py' 2>/dev/null || true"`

5. Show final log lines:
   `ssh trading "tail -10 /tmp/trading.log 2>/dev/null || echo 'No log file'"`

Report the result.
