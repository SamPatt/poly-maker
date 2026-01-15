# AQ Log Analyzer

This tool scans AQ bot logs to estimate adverse selection by comparing fill prices
to contemporaneous mid prices and computing simple markouts.

It relies on two log line types:
- Quote decisions (from `Quote decision ... PLACE_QUOTE`) for mid/bid/ask.
- Fill events (from `Fill: trade=... token=... side=... price=... size=...`).

The analyzer uses full token IDs as logged by the bot.

## Usage

```bash
python tools/aq_log_analyzer.py --log /path/to/aq_bot.log
```

Optional flags:

- `--lookback 30`  
  Max seconds between a quote decision and a fill to consider them related.

- `--horizons 10,30,60`  
  Markout horizons in seconds (using the next mid observation at or after each horizon).

- `--csv /tmp/aq_fills.csv`  
  Write per-fill metrics to a CSV file.

- `--token 85187601979962054323`  
  Filter to a specific token ID (exact match).

- `--require-in-book`  
  Drop fills whose price is outside the matched quote bid/ask. This helps
  reduce token prefix collisions between paired tokens.

Example:

```bash
python tools/aq_log_analyzer.py \
  --log /tmp/aq_bot.log \
  --lookback 20 \
  --horizons 5,15,30 \
  --require-in-book \
  --csv /tmp/aq_fills.csv
```

## Output

The tool prints a per-token summary:

- `fills`: number of fills matched to a recent quote
- `avg_edge_bps`: average edge vs mid at fill time  
  (positive is favorable, negative suggests adverse selection)
- `adverse_rate`: percentage of fills with negative edge

The optional CSV includes:

- `timestamp`, `token`, `side`, `price`, `size`
- `mid_at_fill`, `edge_bps`, `quote_age_s`
- `markout_{h}s_bps` for each horizon

## Limitations

- Uses quote decision logs as a proxy for mid prices, which may be sparse.
- You can mitigate mismatches with `--require-in-book`, which drops fills
  that don't land inside the matched quote spread.
- Markouts are approximate: it selects the first mid observation after the horizon,
  not an exact mid at that timestamp.

If you want more precision, the same logic can be extended to read persisted fills
from the database and use orderbook snapshots instead of quote logs.
