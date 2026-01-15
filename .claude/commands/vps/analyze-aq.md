---
description: Analyze AQ bot logs for adverse selection and profitability
allowed-tools: Bash
---

Run the AQ log analyzer tool on the VPS to analyze fills for adverse selection and markouts.

Execute this command to run the analyzer:
`ssh trading "cd /home/polymaker/poly-maker && source .venv/bin/activate && python tools/aq_log_analyzer.py --log /tmp/aq_bot.log"`

To get detailed CSV output with markout horizons:
`ssh trading "cd /home/polymaker/poly-maker && source .venv/bin/activate && python tools/aq_log_analyzer.py --log /tmp/aq_bot.log --horizons 5,15,30,60 --csv /tmp/fills.csv && cat /tmp/fills.csv"`

To filter to a specific token:
`ssh trading "cd /home/polymaker/poly-maker && source .venv/bin/activate && python tools/aq_log_analyzer.py --log /tmp/aq_bot.log --token TOKEN_PREFIX"`

Report to the user:
- Number of fills analyzed
- Average edge at fill (positive = good entry, negative = adverse selection)
- Adverse selection rate (% of fills with negative edge)
- Markout performance at different time horizons
- Any tokens with particularly bad adverse selection
