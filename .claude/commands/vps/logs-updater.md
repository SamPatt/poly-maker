---
description: View updater logs from updater VPS
argument-hint: [lines]
allowed-tools: Bash
---

View recent logs from the market data updater.

If $ARGUMENTS contains a number, use that as line count. Otherwise default to 50 lines.

Execute:
`ssh updater "tail -${1:-50} /tmp/updater.log"`

Display the logs to the user.
