---
description: Check status of all bots on VPS
allowed-tools: Bash
---

Check the status of all trading bots on the VPS.

Execute these commands and report results:

1. List screen sessions:
   `ssh trading "screen -ls"`

2. Check trading bot process:
   `ssh trading "pgrep -f 'python.*main.py' && echo 'Trading bot: RUNNING' || echo 'Trading bot: STOPPED'"`

3. Check rebates bot process:
   `ssh trading "pgrep -f 'rebates.rebates_bot' && echo 'Rebates bot: RUNNING' || echo 'Rebates bot: STOPPED'"`

4. Check web UI process:
   `ssh trading "pgrep -f 'uvicorn.*app:app' && echo 'Web UI: RUNNING' || echo 'Web UI: STOPPED'"`

5. Show last few lines of each active log:
   - If trading bot running: `ssh trading "echo '--- Trading Bot (last 5 lines) ---' && tail -5 /tmp/trading.log 2>/dev/null || echo 'No log'"`
   - If rebates bot running: `ssh trading "echo '--- Rebates Bot (last 5 lines) ---' && tail -5 /tmp/rebates.log 2>/dev/null || echo 'No log'"`

Summarize the status clearly for the user.
