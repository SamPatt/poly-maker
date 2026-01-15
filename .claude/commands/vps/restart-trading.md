---
description: Pull code and restart trading bot
allowed-tools: Bash
---

Pull latest code and restart the main trading bot on VPS.

Execute these steps:
1. Pull latest code:
   `ssh trading "cd /home/polymaker/poly-maker && git pull origin main"`

2. Stop existing trading bot (screen or stray process):
   `ssh trading "pkill -f '/home/polymaker/poly-maker/main.py' 2>/dev/null || true"`
   `ssh trading "screen -S trading -X quit 2>/dev/null || true"`

3. Wait briefly:
   `sleep 1`

4. Start trading bot:
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS trading bash -c 'source .venv/bin/activate && python -u main.py 2>&1 | tee /tmp/trading.log'"`

5. Verify and show logs:
   `sleep 3 && ssh trading "pgrep -f '/home/polymaker/poly-maker/main.py' -a && tail -20 /tmp/trading.log || echo 'Failed to start'"`

Report whether restart succeeded.
