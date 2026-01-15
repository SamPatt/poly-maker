---
description: Start the Active Quoting bot on VPS
allowed-tools: Bash
---

Start the Active Quoting (AQ) bot on the VPS in a screen session.

The AQ bot automatically discovers and trades 15-minute crypto markets based on AQ_ASSETS in .env.

Execute these commands:

1. Stop any existing AQ bot (screen or stray process):
   `ssh trading "pkill -f 'rebates.active_quoting.bot' 2>/dev/null || true"`
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Wait briefly:
   `sleep 1`

3. Rotate logs (keep last 5):
   `ssh trading "cd /tmp && rm -f aq.log.5 && for i in 4 3 2 1; do [ -f aq.log.\$i ] && mv aq.log.\$i aq.log.\$((i+1)); done; [ -f aq.log ] && mv aq.log aq.log.1; touch aq.log"`

4. Start the AQ bot (using exec so SIGHUP goes directly to Python for graceful shutdown):
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS aq bash -c 'source .venv/bin/activate && exec python -u -m rebates.active_quoting.bot >> /tmp/aq.log 2>&1'"`

5. Wait for startup:
   `sleep 5`

6. Verify it started and show initial logs:
   `ssh trading "pgrep -f 'python -u -m rebates.active_quoting.bot' -a && echo 'AQ bot started successfully' || echo 'Failed to start AQ bot'"`
   `ssh trading "tail -20 /tmp/aq.log"`

Report the result to the user including the discovered markets.
