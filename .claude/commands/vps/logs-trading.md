---
description: View trading bot logs from VPS
argument-hint: [lines]
allowed-tools: Bash
---

View recent logs from the main trading bot.

If $ARGUMENTS contains a number, use that as line count. Otherwise default to 50 lines.

Execute:
`ssh trading "tail -${1:-50} /tmp/trading.log"`

Display the logs to the user.
