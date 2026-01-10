---
description: Start the rebates bot on VPS (live trading)
argument-hint: [--dry-run]
allowed-tools: Bash
---

Start the 15-minute crypto rebates bot on the VPS.

Check if user passed "--dry-run" in $ARGUMENTS:
- If --dry-run: Use REBATES_DRY_RUN=true
- Otherwise: Use REBATES_DRY_RUN=false (live trading)

Execute these commands:
1. Stop any existing rebates bot:
   `ssh trading "screen -S rebates -X quit 2>/dev/null || true"`

2. Wait briefly:
   `sleep 1`

3. Start the rebates bot (adjust REBATES_DRY_RUN based on argument):
   For live: `ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && REBATES_DRY_RUN=false python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"`
   For dry-run: `ssh trading "cd /home/polymaker/poly-maker && screen -dmS rebates bash -c 'source .venv/bin/activate && REBATES_DRY_RUN=true python -u -m rebates.rebates_bot 2>&1 | tee /tmp/rebates.log'"`

4. Verify it started:
   `ssh trading "sleep 2 && pgrep -f 'rebates.rebates_bot' && echo 'Rebates bot started successfully' || echo 'Failed to start rebates bot'"`

5. Show initial log output:
   `ssh trading "sleep 3 && tail -20 /tmp/rebates.log"`

Report the mode (live/dry-run) and result to the user.
