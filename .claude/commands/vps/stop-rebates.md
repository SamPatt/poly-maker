---
description: Stop the rebates bot on VPS
allowed-tools: Bash
---

Stop the rebates bot on the VPS.

Note: The rebates bot doesn't have graceful shutdown handling - it will simply terminate.

Execute:
1. Stop the rebates bot screen session:
   `ssh trading "screen -S rebates -X quit 2>/dev/null || true"`

2. Wait briefly:
   `sleep 2`

3. Verify it stopped:
   `ssh trading "pgrep -f 'rebates_bot' && echo 'Warning: Process still running' || echo 'Rebates bot stopped'"`

4. If still running, force kill:
   `ssh trading "pkill -9 -f 'rebates_bot' 2>/dev/null || true"`

5. Show final log lines:
   `ssh trading "tail -10 /tmp/rebates.log 2>/dev/null || echo 'No log file'"`

Report the result.
