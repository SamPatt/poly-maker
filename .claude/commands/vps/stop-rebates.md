---
description: Stop the rebates bot on VPS
allowed-tools: Bash
---

Stop the rebates bot on the VPS.

Execute:
1. `ssh trading "screen -S rebates -X quit 2>/dev/null || true"`
2. Verify it stopped: `ssh trading "pgrep -f 'rebates.rebates_bot' && echo 'Warning: Process still running' || echo 'Rebates bot stopped'"`

Report the result.
