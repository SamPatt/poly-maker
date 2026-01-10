---
description: Pull code and restart updater on updater VPS
allowed-tools: Bash
---

Pull latest code and restart the market data updater on the updater VPS.

Execute these steps:
1. Pull latest code:
   `ssh updater "cd /home/polymaker/poly-maker && git pull origin main"`

2. Stop existing updater:
   `ssh updater "screen -S updater -X quit 2>/dev/null || true; pkill -f 'python.*update_markets' 2>/dev/null || true"`

3. Wait briefly:
   `sleep 1`

4. Start updater:
   `ssh updater "cd /home/polymaker/poly-maker && screen -dmS updater bash -c 'source .venv/bin/activate && python -u update_markets.py 2>&1 | tee /tmp/updater.log'"`

5. Verify and show logs:
   `ssh updater "sleep 3 && pgrep -f 'update_markets' && tail -15 /tmp/updater.log || echo 'Failed to start'"`

Report whether restart succeeded.
