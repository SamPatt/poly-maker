---
description: View rebates bot logs from VPS
argument-hint: [lines]
allowed-tools: Bash
---

View recent logs from the rebates bot.

If $ARGUMENTS contains a number, use that as line count. Otherwise default to 50 lines.

Execute:
`ssh trading "tail -${1:-50} /tmp/rebates.log"`

Display the logs to the user and summarize any errors or important events.
