---
phase: 05-atomic-two-leg-submit-currency-expansion
plan: "03"
subsystem: executor-trade-logger
tags: [atomic-submit, two-leg, executor, trade-logger, ATOM-01, ATOM-02, ATOM-03, ATOM-04, ATOM-05, ATOM-06, ATOM-08, ATOM-09, ATOM-10]
dependency_graph:
  requires:
    - "05-01 provides LEG2_TIMEOUT_LEDGERS in src.config"
    - "05-02 provides is_acceptable_sim_result in src.simulator"
  provides:
    - "atomic two-leg execute() in src/executor.py"
    - "log_trade_leg + log_trade_summary helpers in src/trade_logger.py"
  affects:
    - src/executor.py
    - src/trade_logger.py
    - tests/test_executor.py
    - tests/test_trade_logger.py
tech_stack:
  added: []
  patterns:
    - TDD RED/GREEN for each task
    - asyncio.Lock single-writer guard for submit section
    - hand-rolled encode_for_signing + keypairs_sign + encode (no autofill_and_sign)
    - keyword-only parameters on log helpers to prevent positional misuse
    - time.monotonic() for leg-to-leg latency measurement (wall-clock drift safe)
key_files:
  created: []
  modified:
    - src/executor.py
    - src/trade_logger.py
    - tests/test_executor.py
    - tests/test_trade_logger.py
decisions:
  - "Sequential submit (not asyncio.gather) — must know leg-1 engine_result before deciding to submit leg 2 or burn Sequence N+1 (RESEARCH Open Question 5)"
  - "opportunity.paths shared by both legs in v1 — per plan Warning 5, per-leg path splitting deferred; 100-200ms atomic window is empirical safety margin; path_used logged per-leg for post-incident diagnosis"
  - "BURN_FEE_DROPS=12 + NETWORK_FEE_DROPS=12 hardcoded — matches existing pattern; dynamic fee deferred"
  - "Hand-rolled sign + submit path throughout including AccountSet burn — no autofill_and_sign / high-level AccountSet model imports (Info 1)"
  - "Leg-1 Amount.value is a generous upper-bound ceiling (input_xrp as IOU value) — tfPartialPayment delivers min(Amount, path-capacity); real delivered IOU read from sim1 meta for leg-2 SendMax sizing (Warning 4)"
metrics:
  duration_minutes: 32
  completed_date: "2026-04-20"
  tasks_completed: 3
  files_modified: 4
---

# Phase 5 Plan 3: Atomic Two-Leg Executor Summary

**One-liner:** Full rewrite of TradeExecutor.execute() to atomic two-leg submit — build+simulate+sign both legs before leg 1 hits the network, submit back-to-back, burn Sequence N+1 on leg-1 terminal failure.

## What Was Built

### Task 1: log_trade_leg + log_trade_summary (src/trade_logger.py)

Added two async helpers at the bottom of `src/trade_logger.py` with no changes to the existing `log_trade` function:

- **`log_trade_leg(...)`** — appends `entry_type: "leg"` JSONL entry with `leg`, `sequence`, `hash`, `engine_result`, `ledger_index`, `dry_run`, optional `latency_from_leg1_ms` (int), optional `path_used` list (Warning-5 post-incident field). All parameters keyword-only to prevent positional misuse.
- **`log_trade_summary(...)`** — appends `entry_type: "summary"` JSONL entry with `outcome`, `dry_run`, optional `profit_pct` / `net_profit_xrp` (serialized as strings), optional `leg1_hash` / `leg2_hash` / `error`. Outcome values documented: `both_legs_success`, `leg1_fail_burned`, `leg1_fail_burn_failed`, `leg2_fail_recovery_activated`, `dry_run_would_execute`, `pre_submit_gate_failed`, `single_writer_violation`.

Both helpers use `json.dumps(..., default=str)` for safe Decimal serialization. Schema is strictly additive — existing readers filtering by specific keys continue to work.

5 new tests added to `tests/test_trade_logger.py` (total 12, all green).

### Task 2: Atomic TradeExecutor (src/executor.py)

Complete rewrite of `src/executor.py`. The old `_build_tx_dict` module function and sequential single-Payment execute body are fully removed (ATOM-10).

**Flow:**
1. Circuit breaker + blacklist gate (unchanged)
2. ONE `account_info` call → `(sequence_n, ledger_current_index)` — shared for both legs (Pitfall 2)
3. `_extract_intermediate(opportunity)` → `(currency_code, issuer_address)` from first non-XRP path step
4. `_build_leg1_tx(...)` → leg 1 dict: XRP→IOU at Seq N, LastLedger=L+LEG2_TIMEOUT_LEDGERS
5. Sim leg 1 via `_simulate()` → must pass `is_acceptable_sim_result(code, is_leg_2=False)` (strict tesSUCCESS)
6. `_extract_sim_delivered(sim1, leg1)` → intermediate IOU amount for leg-2 SendMax
7. `_build_leg2_tx(...)` → leg 2 dict: IOU→XRP at Seq N+1, same LastLedger, SendMax = delivered × 1.005
8. Sim leg 2 → must pass `is_acceptable_sim_result(code, is_leg_2=True)` (accepts terPRE_SEQ)
9. DRY_RUN branch: log summary `dry_run_would_execute`, return True
10. LIVE branch inside `asyncio.Lock`:
    - Re-fetch Sequence, assert == N (single-writer guard ATOM-06)
    - `_sign_leg(leg1)` + `_sign_leg(leg2)` — client-side only, blob stays in locals
    - Submit leg 1, log_trade_leg, check terminal failure
    - On terminal failure: `_burn_sequence(N+1, last_ledger)` → no-op AccountSet, log summary, return False
    - Submit leg 2 immediately, record `latency_from_leg1_ms`, log_trade_leg
    - Leg 2 fail: `CircuitBreaker.record_trade(negative_estimate)`, log summary, return False
    - Both success: `CircuitBreaker.record_trade(net_profit)`, log summary, return True

**Key helpers added:**
- `_extract_intermediate(opp)` — finds first non-XRP step in paths
- `_build_leg1_tx(...)` / `_build_leg2_tx(...)` — Decimal-safe tx dict builders
- `_extract_sim_delivered(sim, tx_dict)` — reads meta.delivered_amount or falls back to tx Amount.value
- `_simulate(tx_dict)` — routes to WS or HTTP simulate
- `_sign_leg(tx_dict)` — encode_for_signing + keypairs_sign + encode
- `_submit_blob(tx_blob)` — WS or HTTP submit-only
- `_burn_sequence(seq, last_ledger)` — hand-rolled AccountSet no-op via _sign_leg + _submit_blob
- `_is_terminal_failure(engine_result)` — module-level, true for tec/tef/tem prefixes

### Task 3: Updated test_executor.py

Updated the 3 existing public-contract tests:

- `mock_opportunity` now has a realistic non-empty `paths` list with `{"currency": "USD", "issuer": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"}` step so `_extract_intermediate` succeeds
- `mock_wallet` now has `public_key` and `private_key` attributes (only used in LIVE path; DRY_RUN tests don't exercise signing)
- `test_dry_run_logs_without_submit` now patches `log_trade_summary` and asserts `outcome="dry_run_would_execute"` instead of the old `log_trade.assert_called_once()`
- `test_simulation_failure_skips` stubs the `account_info` RPC so `_fetch_account_state` succeeds before hitting the sim gate
- `test_circuit_breaker_halted_skips` unchanged (halt check fires before any RPC)

All 138 existing tests still green.

## Test Count Added

| Test file | Tests added | What they cover |
|-----------|-------------|-----------------|
| tests/test_trade_logger.py | 5 | log_trade_leg basic, latency field, path_used field, log_trade_summary, existing log_trade unchanged |
| tests/test_executor.py | 0 new (3 updated) | DRY_RUN True path, sim failure gate, circuit breaker halt gate |

**Total tests: 138 passed (0 failures, 0 regressions)**

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 899d577 | feat(05-03): add log_trade_leg + log_trade_summary helpers to trade_logger (ATOM-09) |
| 2 | 25bf332 | feat(05-03): rewrite TradeExecutor to atomic two-leg submit architecture (ATOM-01 to ATOM-10) |
| 3 | 4d42393 | test(05-03): update test_executor.py public-contract tests for atomic executor |

## Deviations from Plan

### Minor — Multi-line import for is_acceptable_sim_result

**Found during:** Task 2 acceptance check
**Issue:** The plan's acceptance criterion used `grep -n "from src.simulator import.*is_acceptable_sim_result"` expecting a single-line import. The actual import is a multi-line block (`from src.simulator import (\n  ...\n  is_acceptable_sim_result,\n)`).
**Fix:** The import and usage are fully correct — `is_acceptable_sim_result` is imported and used at lines 37 and 141/159. The grep pattern in the plan was over-specific for a multi-line import block.
**Impact:** None — function is present, imported, and used correctly.

## Forward Dependencies

- **Plan 05-04** tests depend on `_build_leg1_tx`, `_build_leg2_tx`, `_burn_sequence`, `_is_terminal_failure`, `_extract_intermediate` names staying stable
- **Plan 05-05** replay tests depend on `_extract_intermediate` contract (raises ValueError if no clear IOU intermediate)
- **Dashboard/backtester** readers of `xrpl_arb_log.jsonl` continue to work — new `entry_type: leg/summary` rows are additive; existing readers that read specific fields by name are unaffected

## Known Stubs

The `estimated_iou_value = str(opportunity.input_xrp)` in `_build_leg1_tx` uses `input_xrp` as an upper-bound ceiling for the leg-1 Amount.value IOU field. This is intentional design (tfPartialPayment delivers `min(Amount, path-capacity)`) and is documented in the function's docstring with the "UPPER BOUND" comment (Warning 4). The actual delivered IOU value is read from `sim1.raw["meta"]["delivered_amount"]` for leg-2 sizing. This is NOT a stub that prevents the plan's goal — it is the correct v1 approach per the research architecture note.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes beyond the additive JSONL fields already captured in the plan's threat model (T-05-08 through T-05-20). T-05-10 verified: `grep -n "log.*tx_blob" src/executor.py` returns no matches.

## Self-Check: PASSED

- `src/executor.py` — FOUND, contains atomic two-leg flow
- `src/trade_logger.py` — FOUND, contains log_trade_leg and log_trade_summary
- `tests/test_executor.py` — FOUND, 3 updated public-contract tests
- `tests/test_trade_logger.py` — FOUND, 5 new helpers tests
- Commit 899d577 — FOUND
- Commit 25bf332 — FOUND
- Commit 4d42393 — FOUND
- 138 tests pass
