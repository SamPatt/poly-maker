---
description: Start the Active Quoting bot on VPS
allowed-tools: Bash
---

Start the Active Quoting (AQ) bot on the VPS in a screen session.

The AQ bot automatically discovers and trades 15-minute crypto markets based on AQ_ASSETS in .env.

Execute these commands:

1. Stop any existing AQ bot:
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Wait briefly:
   `sleep 1`

3. Start the AQ bot:
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS aq bash -c 'source .venv/bin/activate && python -m rebates.active_quoting.bot 2>&1 | tee /tmp/aq.log'"`

4. Wait for startup:
   `sleep 5`

5. Verify it started and show initial logs:
   `ssh trading "pgrep -f 'rebates.active_quoting.bot' && echo 'AQ bot started successfully' || echo 'Failed to start AQ bot'"`
   `ssh trading "tail -20 /tmp/aq.log"`

Report the result to the user including the discovered markets.
