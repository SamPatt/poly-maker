---
description: Pull code and restart Active Quoting bot on VPS
allowed-tools: Bash
---

Pull the latest code and restart the Active Quoting (AQ) bot on the VPS.

Execute these commands:

1. Stop any existing AQ bot (screen or stray process):
   `ssh trading "pkill -f 'rebates.active_quoting.bot' 2>/dev/null || true"`
   `ssh trading "screen -S aq -X quit 2>/dev/null || true"`

2. Pull latest code:
   `ssh trading "cd /home/polymaker/poly-maker && git pull origin main"`

3. Wait briefly:
   `sleep 1`

4. Rotate logs (keep last 5):
   `ssh trading "cd /tmp && rm -f aq.log.5 && for i in 4 3 2 1; do [ -f aq.log.\$i ] && mv aq.log.\$i aq.log.\$((i+1)); done; [ -f aq.log ] && mv aq.log aq.log.1; touch aq.log"`

5. Start the AQ bot (appending to log):
   `ssh trading "cd /home/polymaker/poly-maker && screen -dmS aq bash -c 'source .venv/bin/activate && python -m rebates.active_quoting.bot 2>&1 | tee -a /tmp/aq.log'"`

6. Wait for startup:
   `sleep 5`

7. Verify it started and show initial logs:
   `ssh trading "pgrep -f 'python -u -m rebates.active_quoting.bot' -a && echo 'AQ bot restarted successfully' || echo 'Failed to restart AQ bot'"`
   `ssh trading "tail -20 /tmp/aq.log"`

Report the result to the user.
