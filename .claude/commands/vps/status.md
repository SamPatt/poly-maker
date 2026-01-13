---
description: Check status of all bots on both VPS servers
allowed-tools: Bash
---

Check the status of all trading bots on both VPS servers.

Execute these commands and report results:

**Trading VPS (ssh trading):**

1. List screen sessions:
   `ssh trading "screen -ls 2>/dev/null || echo 'No screens'"`

2. Check trading bot process:
   `ssh trading "pgrep -f 'python.*main.py' && echo 'Trading bot: RUNNING' || echo 'Trading bot: STOPPED'"`

3. Check rebates bot process:
   `ssh trading "pgrep -f 'rebates.rebates_bot' && echo 'Rebates bot: RUNNING' || echo 'Rebates bot: STOPPED'"`

4. Check AQ bot process:
   `ssh trading "pgrep -f 'rebates.active_quoting.bot' && echo 'AQ bot: RUNNING' || echo 'AQ bot: STOPPED'"`

5. Check web UI process:
   `ssh trading "pgrep -f 'uvicorn.*app:app' && echo 'Web UI: RUNNING' || echo 'Web UI: STOPPED'"`

6. Show last few lines of active logs:
   `ssh trading "echo '--- Trading Bot ---' && tail -3 /tmp/trading.log 2>/dev/null || echo 'No log'"`
   `ssh trading "echo '--- Rebates Bot ---' && tail -3 /tmp/rebates.log 2>/dev/null || echo 'No log'"`
   `ssh trading "echo '--- AQ Bot ---' && tail -3 /tmp/aq.log 2>/dev/null || echo 'No log'"`

**Updater VPS (ssh updater):**

7. Check updater process:
   `ssh updater "pgrep -f 'update_markets' && echo 'Updater: RUNNING' || echo 'Updater: STOPPED'"`

8. Show updater log:
   `ssh updater "echo '--- Updater ---' && tail -3 /tmp/updater.log 2>/dev/null || echo 'No log'"`

Summarize the status clearly for the user in a table format.
