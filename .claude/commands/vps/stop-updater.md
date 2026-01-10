---
description: Stop the data updater on updater VPS
allowed-tools: Bash
---

Stop the market data updater on the updater VPS.

Execute:
1. Stop screen and kill processes:
   `ssh updater "screen -S updater -X quit 2>/dev/null || true; pkill -f 'python.*update_markets' 2>/dev/null || true"`

2. Verify it stopped:
   `ssh updater "sleep 1 && pgrep -f 'update_markets' && echo 'Warning: Process still running' || echo 'Updater stopped'"`

Report the result.
