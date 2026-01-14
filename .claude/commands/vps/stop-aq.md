---
description: Stop the Active Quoting bot on VPS
allowed-tools: Bash
---

Stop the Active Quoting (AQ) bot on the VPS with graceful shutdown.

The bot handles SIGHUP from screen quit, allowing it to:
- Cancel all open orders
- Send Telegram shutdown alert
- Save state to database

Execute these commands:

1. Stop the AQ bot screen session (sends SIGHUP for graceful shutdown):
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Wait for graceful shutdown (cancelling orders, sending alerts):
   `sleep 5`

3. Verify it stopped:
   `ssh trading "pgrep -f 'rebates.active_quoting.bot' && echo 'AQ bot still running (may need force kill)' || echo 'AQ bot stopped successfully'"`

4. If still running, force kill:
   `ssh trading "pkill -9 -f 'rebates.active_quoting.bot' 2>/dev/null || true"`

5. Show final log lines (should show "Bot stopped" message):
   `ssh trading "tail -15 /tmp/aq.log 2>/dev/null || echo 'No log file'"`

Report the result to the user. The logs should show order cancellation and "Bot stopped" if graceful shutdown succeeded.
