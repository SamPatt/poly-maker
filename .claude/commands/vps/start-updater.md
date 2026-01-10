---
description: Start the data updater on updater VPS
allowed-tools: Bash
---

Start the market data updater on the updater VPS in a screen session.

Execute these commands:
1. Stop any existing updater:
   `ssh updater "screen -S updater -X quit 2>/dev/null || true; pkill -f 'python.*update_markets' 2>/dev/null || true"`

2. Wait briefly:
   `sleep 1`

3. Start the updater:
   `ssh updater "cd /home/polymaker/poly-maker && screen -dmS updater bash -c 'source .venv/bin/activate && python -u update_markets.py 2>&1 | tee /tmp/updater.log'"`

4. Verify it started:
   `ssh updater "sleep 2 && pgrep -f 'update_markets' && echo 'Updater started successfully' || echo 'Failed to start updater'"`

Report the result to the user.
