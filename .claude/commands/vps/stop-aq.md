---
description: Stop the Active Quoting bot on VPS
allowed-tools: Bash
---

Stop the Active Quoting (AQ) bot on the VPS.

Execute these commands:

1. Stop the AQ bot screen session:
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Verify it stopped:
   `ssh trading "pgrep -f 'rebates.active_quoting.bot' && echo 'AQ bot still running (may need force kill)' || echo 'AQ bot stopped successfully'"`

3. Show final log lines:
   `ssh trading "tail -10 /tmp/aq.log 2>/dev/null || echo 'No log file'"`

Report the result to the user.
