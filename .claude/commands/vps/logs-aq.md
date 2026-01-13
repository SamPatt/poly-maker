---
description: View Active Quoting bot logs from VPS
allowed-tools: Bash
---

View the Active Quoting (AQ) bot logs from the VPS.

Execute this command to show recent logs:
`ssh trading "tail -100 /tmp/aq.log 2>/dev/null || echo 'No AQ log file found'"`

Summarize the key information for the user:
- Bot status (running/stopped)
- Number of markets discovered
- Any errors or warnings
- Recent fills if any
- Circuit breaker state
