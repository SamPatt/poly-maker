---
description: Stop the main trading bot on VPS
allowed-tools: Bash
---

Stop the main trading bot on the VPS.

Execute:
1. `ssh trading "screen -S trading -X quit 2>/dev/null || true"`
2. Verify it stopped: `ssh trading "pgrep -f 'python.*main.py' && echo 'Warning: Process still running' || echo 'Trading bot stopped'"`

Report the result.
