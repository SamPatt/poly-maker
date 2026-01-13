---
description: Pull code and restart Active Quoting bot on VPS
allowed-tools: Bash
---

Pull the latest code and restart the Active Quoting (AQ) bot on the VPS.

Execute these commands:

1. Stop any existing AQ bot:
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Pull latest code:
   `ssh trading "cd /home/polymaker/poly-maker && git pull origin main"`

3. Wait briefly:
   `sleep 1`

4. Start the AQ bot:
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS aq bash -c 'source .venv/bin/activate && python -m rebates.active_quoting.bot 2>&1 | tee /tmp/aq.log'"`

5. Wait for startup:
   `sleep 5`

6. Verify it started and show initial logs:
   `ssh trading "pgrep -f 'rebates.active_quoting.bot' && echo 'AQ bot restarted successfully' || echo 'Failed to restart AQ bot'"`
   `ssh trading "tail -20 /tmp/aq.log"`

Report the result to the user.
