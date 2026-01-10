---
description: Commit, push, and deploy code to VPS
argument-hint: [commit-message]
allowed-tools: Bash
---

Deploy code changes to the trading VPS.

Steps to perform:
1. Stage all changes: `git add -A`
2. Commit with message: "$ARGUMENTS" (or ask for message if not provided)
3. Push to origin: `git push origin main`
4. Pull on VPS: `ssh trading "cd /home/polymaker/poly-maker && git pull origin main"`

After deployment, report:
- Files changed
- Commit hash
- Whether VPS pull succeeded

If any step fails, stop and report the error.
