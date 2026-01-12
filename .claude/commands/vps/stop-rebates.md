---
description: Stop the rebates bot on VPS
allowed-tools: Bash
---

Stop the rebates bot on the VPS.

Execute:
1. `ssh trading "screen -S rebates -X quit 2>/dev/null || true"`
2. Verify it stopped: `ssh trading "ps aux | grep 'rebates_bot' | grep python | grep -v grep && echo 'Warning: Process still running' || echo 'Rebates bot stopped'"`
3. If process still running, kill it: `ssh trading "pkill -f 'python.*rebates_bot' 2>/dev/null || true"` and verify again.

Report the result.
