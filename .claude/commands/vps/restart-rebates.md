---
description: Pull code and restart rebates bot
argument-hint: [--dry-run]
allowed-tools: Bash
---

Pull latest code and restart the rebates bot on VPS.

Check if user passed "--dry-run" in $ARGUMENTS:
- If --dry-run: Use REBATES_DRY_RUN=true
- Otherwise: Use REBATES_DRY_RUN=false (live trading)

Execute these steps:
1. Pull latest code:
   `ssh trading "cd /home/polymaker/poly-maker && git pull origin main"`

2. Stop existing rebates bot:
   `ssh trading "screen -S rebates -X quit 2>/dev/null || true"`

3. Wait briefly:
   `sleep 1`

4. Start rebates bot (with appropriate DRY_RUN setting):
   For live: `ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && REBATES_DRY_RUN=false python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"`
   For dry-run: `ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && REBATES_DRY_RUN=true python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"`

5. Wait and show logs:
   `sleep 5 && ssh trading "tail -30 /tmp/rebates.log"`

Report the mode (live/dry-run) and whether restart succeeded.
