# On-Chain Inventory Plan

## Goal
Make on-chain ERC-1155 balances the authoritative source of truth for inventory, and remove reliance on REST API snapshots and the discrepancy halt. The bot should keep trading through short-term data lag, while still managing risk conservatively.

## Current Pain Points
- REST API position snapshots lag or diverge from websocket fills.
- The discrepancy halt frequently pauses markets, hurting profitability.
- We reconcile across sources that can both be stale.

## Target State
- Authoritative inventory = on-chain token balances (CTF ERC-1155).
- Websocket fills are treated as optimistic, short-lived deltas only.
- REST API becomes a secondary fallback (health/debug only).
- Discrepancy halts are replaced with conservative quoting and reconciliation.

## Proposed Architecture
### 1) On-chain position provider
Create an on-chain snapshot service that returns balances for the token_ids we trade.

- Contract: Conditional Tokens Framework (ERC-1155) on Polygon.
- Method: `balanceOf(address, tokenId)` (or batch `balanceOfBatch`).
- Use wallet address that holds inventory (funder or trading wallet).

Suggested class:
- `rebates/active_quoting/onchain_position_provider.py`
  - `fetch_balances(token_ids) -> Dict[token_id, size]`
  - Uses multicall or `balanceOfBatch` for efficiency.
  - Configurable provider URL, chain id, and polling interval.

### 2) InventoryManager changes
Refactor `InventoryManager` to accept a new authoritative snapshot source:

- Replace `confirmed_size` meaning: now equals latest on-chain balance.
- `pending_fills` remain, but only used for "effective_size" and short-term UI/PnL.
- Pending fills are cleared when on-chain balances absorb them, or after a safety TTL.

Key behavior:
- On each on-chain sync: `confirmed_size = onchain_balance`.
- Reconcile pending fills:
  - If on-chain increased by X, absorb BUY pending fills up to X.
  - If on-chain decreased by X, absorb SELL pending fills up to X.
  - If on-chain moves opposite to pending, keep logging and age out pending.

### 3) Bot sync loop changes
Replace `_sync_positions_from_api()` usage with on-chain sync:

- New `_sync_positions_from_chain(token_ids)`:
  - Fetch on-chain balances for active token_ids.
  - Call `inventory_manager.set_position()` with on-chain values.
  - Logs differences between on-chain vs effective_size for debugging only.

- REST API remains optional fallback:
  - If on-chain provider is down, use API snapshot but mark it as degraded.
  - Emit warning and tighten quoting (smaller size, wider quotes).

### 4) Replace discrepancy halt with degrade mode
Instead of halting on discrepancy:

- If `abs(effective - confirmed) > threshold` for duration:
  - Reduce size (e.g., 25-50%).
  - Quote only on the side that reduces inventory.
  - Widen offsets by +1-2 ticks.

Reserve hard halt only for:
- On-chain sync unavailable for extended period (e.g., > 5-10 minutes).
- Balances negative or impossible.

### 5) Data quality protections
- Sequence gap detection on websocket stream:
  - If a gap occurs, trigger immediate on-chain snapshot.
- WS "MATCHED" is not final:
  - Only treat a fill as pending until on-chain balance changes.
- Safety TTL for pending fills (e.g., 2-5 minutes):
  - After TTL, drop pending fill and log a warning.

## Config Additions
- `AQ_ONCHAIN_PROVIDER_URL`
- `AQ_ONCHAIN_SYNC_INTERVAL_SECONDS` (default 2-5s, tune per RPC limits)
- `AQ_ONCHAIN_SYNC_TIMEOUT_SECONDS`
- `AQ_ONCHAIN_DEGRADE_THRESHOLD_SHARES` (global default)
- `AQ_ONCHAIN_DEGRADE_THRESHOLD_SHARES_PER_MARKET` (optional per-market override)
- `AQ_ONCHAIN_HARD_HALT_SECONDS` (fallback only)
- `AQ_PENDING_FILL_TTL_SECONDS`
- `AQ_ONCHAIN_MULTI_CALL_ENABLED`

## Implementation Phases
### Phase 1: Read-only on-chain snapshot (no behavior changes)
- Add on-chain provider and log on-chain vs API discrepancies.
- No trading changes yet.
- Goal: verify data accuracy and latency.

### Phase 2: Switch authoritative source to on-chain
- Replace `confirmed_size` updates with on-chain balances.
- Keep REST API for fallback only.
- Keep discrepancy logic but do not halt; only log.

### Phase 3: Degrade mode instead of halt
- Implement conservative quoting response when discrepancy persists.
- Remove normal discrepancy halt; keep only hard halt for loss of on-chain sync.

### Phase 4: Cleanup
- Remove now-unused API discrepancy hacks.
- Keep an optional manual API reconciliation path for debugging.

### Optional Phase: Event-driven balance updates
- Subscribe to ERC-1155 `TransferSingle` / `TransferBatch` for the wallet.
- Use events to update confirmed balances with minimal latency.
- Keep polling as a safety net in case of missed events.

## Testing Plan
- Unit tests for pending fill absorption vs on-chain deltas.
- Tests for degrade mode activation and recovery.
- Simulated gaps in WS feed and on-chain sync failure.
- Integration test: mock on-chain provider returns sequence of balances while fills arrive.

## Risks and Mitigations
- On-chain RPC latency: mitigate with batch calls + caching.
- RPC outage: fallback to API with conservative quoting and reduced size.
- Token ID mismatch: ensure full token IDs are used everywhere.

## Rollout Checklist
- Run Phase 1 in dry-run for 1-2 sessions.
- Compare on-chain vs API vs WS for several markets.
- If stable, enable Phase 2 with reduced sizes.
- After 1-2 days, enable Phase 3 degrade mode and remove frequent halts.

## Open Questions
- Which wallet holds inventory (funder vs trading signer)? Ensure it is the address that actually receives ERC-1155 balances on fill.
- Preferred RPC provider and rate limits?
- Acceptable sync interval given RPC cost?
