---
description: Start the main trading bot on VPS
allowed-tools: Bash
---

Start the main trading bot on the VPS in a screen session.

Execute these commands:
1. Stop any existing trading bot (screen or stray process):
   `ssh trading "pkill -f '/home/polymaker/poly-maker/main.py' 2>/dev/null || true"`
   `ssh trading "screen -S trading -X quit 2>/dev/null || true"`

2. Wait briefly:
   `sleep 1`

3. Start the trading bot:
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS trading bash -c 'source .venv/bin/activate && python -u main.py 2>&1 | tee /tmp/trading.log'"`

4. Verify it started:
   `ssh trading "sleep 2 && pgrep -f '/home/polymaker/poly-maker/main.py' -a && echo 'Trading bot started successfully' || echo 'Failed to start trading bot'"`

Report the result to the user.
