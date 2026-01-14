---
description: View Active Quoting bot logs from VPS
allowed-tools: Bash
---

View the Active Quoting (AQ) bot logs from the VPS.

Execute this command to show recent logs:
`ssh trading "tail -100 /tmp/aq.log 2>/dev/null || echo 'No AQ log file found'"`

To view older rotated logs (aq.log.1 through aq.log.5):
`ssh trading "cat /tmp/aq.log.1 2>/dev/null | tail -100"`

Summarize the key information for the user:
- Bot status (running/stopped)
- Number of markets discovered
- Any errors or warnings
- Recent fills if any
- Circuit breaker state
