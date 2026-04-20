---
phase: 05-atomic-two-leg-submit-currency-expansion
plan: "04"
subsystem: test-atomic-executor
tags: [tests, atomic-submit, two-leg, ATOM-01, ATOM-02, ATOM-03, ATOM-04, ATOM-05, ATOM-06, ATOM-07, ATOM-08, ATOM-09]
dependency_graph:
  requires:
    - "05-03 provides TradeExecutor, _is_terminal_failure, _extract_intermediate, keypairs_sign import"
  provides:
    - "tests/test_atomic_executor.py — 16 tests covering ATOM-01 through ATOM-09"
    - "tests/conftest.py — shared fixtures (mock_ws_connection, atomic_opportunity, sim_factory)"
  affects:
    - tests/test_atomic_executor.py
    - tests/conftest.py
tech_stack:
  added: []
  patterns:
    - TDD: tests written against Plan 05-03 executor
    - FIFO mock WS response dispatch table (per-command queue in conftest)
    - Real xrpl.core.binarycodec.decode for tx_blob inspection (T-05-14 threat mitigation)
    - keypairs_sign spy via patch() for signing-order assertion (ATOM-01 sign-ordering half)
    - pytest-asyncio for async test functions
    - Keyword-only fixture access via pytest argument injection
key_files:
  created:
    - tests/test_atomic_executor.py
    - tests/conftest.py
  modified: []
decisions:
  - "ATOM-01 split into two narrow tests: test_both_legs_simulated_before_first_submit (simulate ordering) + test_both_legs_signed_before_first_submit (signing ordering) — per plan-checker Warning 3, old over-promising name test_atomic_both_legs_presigned_before_submit retired"
  - "test_atomic_sequences_are_n_and_n_plus_1 and test_atomic_all_amounts_are_decimal use real xrpl.core.binarycodec.decode to inspect actual tx_blob contents — catches executor bugs in tx dict construction, not just mock echoes (T-05-14)"
  - "test_leg2_terPRE_SEQ_treated_as_pass uses dry_run=True to avoid needing submit mocks — the sim gate pass-through is what matters for ATOM-07 e2e, not the actual submission path"
  - "conftest.py fixtures (mock_wallet, mock_circuit_breaker, mock_blacklist) supersede the same-named local fixtures in tests/test_executor.py — no conflict since conftest fixtures are opt-in by argument name and test_executor.py defines its own local versions"
metrics:
  duration_minutes: 5
  completed_date: "2026-04-20"
  tasks_completed: 3
  files_modified: 2
---

# Phase 5 Plan 4: Atomic Executor Test Suite Summary

**One-liner:** 16-test pytest suite covering ATOM-01 to ATOM-09 — simulate ordering, signing ordering, Sequence N/N+1 contract, burn-on-failure, single-writer guard, Decimal preservation, and per-leg logging verified via real binary-codec inspection.

## What Was Built

### Task 1: tests/conftest.py (shared fixtures)

Created `tests/conftest.py` with shared fixtures and response-builder helpers consumed by `test_atomic_executor.py` (and reusable by Plan 05-05's `test_replay_incident.py`):

- **`mock_wallet`** — MagicMock with `.address`, `.public_key` ("ED" + "00"*32), `.private_key` ("ED" + "11"*32); compatible with the real `keypairs_sign` / `encode_for_signing` pipeline
- **`mock_circuit_breaker`** — `is_halted()=False`, `record_trade` MagicMock
- **`mock_blacklist`** — `is_blacklisted()=False`
- **`atomic_opportunity`** — Opportunity with `input_xrp=5`, `output_xrp=5.05`, USD paths via rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq issuer
- **`mock_ws_connection`** — MagicMock with `connected=True`; `send_raw` is an AsyncMock that dispatches to a per-command FIFO queue (`conn.responses["account_info"] = [...]`) and logs every call to `send_raw_call_log`
- **`sim_factory`** — fixture exposing `account_info_response`, `simulate_response`, `submit_response` dict builders
- **`account_info_response(sequence, ledger)`** — module-level helper (importable by name for direct use in tests)
- **`simulate_response(engine_result, delivered_iou=None)`** — builds result with optional `meta.delivered_amount` IOU dict
- **`submit_response(engine_result, tx_hash)`** — builds canonical submit result

Collection check: 138 existing tests still discoverable; 0 import errors.

### Task 2: Happy-path + sim-gate + signing-order tests (ATOM-01, 02, 03, 07)

7 async test functions in `tests/test_atomic_executor.py`:

| Test | REQ | What it asserts |
|------|-----|-----------------|
| `test_both_legs_simulated_before_first_submit` | ATOM-01 (sim half) | commands[:first_submit].count("simulate") == 2 |
| `test_both_legs_signed_before_first_submit` | ATOM-01 (sign half) | keypairs_sign spy fires >=2 times before first send_raw:submit in shared event_log |
| `test_atomic_sequences_are_n_and_n_plus_1` | ATOM-02 | xrpl_decode(tx_blob)["Sequence"] == N and N+1; same LastLedgerSequence |
| `test_atomic_leg2_submits_before_leg1_validates` | ATOM-03 | No "tx" or "submit_and_wait" between the two submit commands |
| `test_leg1_sim_rejection_aborts_before_submit` | sim gate | leg-1 tecPATH_DRY → no submit, outcome="pre_submit_gate_failed" |
| `test_leg2_sim_rejection_aborts_before_submit` | sim gate | leg-2 tecPATH_PARTIAL → no submit |
| `test_leg2_terPRE_SEQ_treated_as_pass` | ATOM-07 e2e | terPRE_SEQ on leg-2 sim → dry_run_would_execute, result=True |

### Task 3: Failure/recovery/single-writer/Decimal tests (ATOM-04, 05, 06, 08, 09)

8 additional test functions (7 async + 2 sync helpers, +1 edge case):

| Test | REQ | What it asserts |
|------|-----|-----------------|
| `test_leg1_terminal_fail_burns_sequence` | ATOM-04 | xrpl_decode(submits[1]["tx_blob"])["TransactionType"] == "AccountSet", Sequence == N+1, outcome="leg1_fail_burned" |
| `test_leg1_terminal_fail_burn_also_fails_reports_escalation` | ATOM-04 edge | burn submit returns terRETRY → outcome="leg1_fail_burn_failed" |
| `test_leg2_fail_activates_existing_recovery` | ATOM-05 | record_trade called with isinstance(arg, Decimal) and arg < 0, outcome="leg2_fail_recovery_activated" |
| `test_single_writer_guard_rejects_concurrent` | ATOM-06 | Sequence 100→105 drift → no submit, outcome="single_writer_violation" |
| `test_atomic_all_amounts_are_decimal` | ATOM-08 | Walk all decoded tx_blob values; none are float |
| `test_atomic_per_leg_log_entries` | ATOM-09 | log_trade_leg called 2x; legs {1,2}; sequence/hash/engine_result/ledger_index/dry_run present; leg1 latency=None, leg2 latency=int |
| `test_terminal_failure_helper_classifies_correctly` | helper | tec/tef/tem → True; tes/ter/tel/unknown → False |
| `test_extract_intermediate_parses_opportunity_paths` | helper | Returns first non-XRP currency+issuer |
| `test_extract_intermediate_raises_on_xrp_only_paths` | helper | Raises ValueError for XRP-only path |

## Test Count Summary

| File | Tests added | Runtime |
|------|-------------|---------|
| tests/conftest.py | 0 (fixtures only) | — |
| tests/test_atomic_executor.py | 16 | 0.29s |

**Total tests: 154 passed (138 original + 16 new, 0 failures, 0 regressions)**

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 668302f | feat(05-04): add shared atomic-executor fixtures to tests/conftest.py |
| 2+3 | 3d24ec6 | test(05-04): add atomic executor tests covering ATOM-01 through ATOM-09 |

## Deviations from Plan

### Minor — Tasks 2 and 3 committed together

**Found during:** Task commit protocol
**Issue:** Tasks 2 and 3 both write to the same file (`tests/test_atomic_executor.py`). The plan shows them as TDD steps but the file contains all tests from both tasks simultaneously. Rather than artificially splitting into two commits for the same file, a single commit covers both tasks.
**Impact:** None — all tests pass, all acceptance criteria met.

### None (content) — Plan executed exactly as written

All test names, assertions, fixture shapes, and REQ-ID mappings implemented exactly per the plan specification. The `test_atomic_both_legs_presigned_before_submit` old name is absent; both ATOM-01 split tests are present with the correct narrowly-scoped names.

## Forward Dependencies

- **Plan 05-05** (`test_replay_incident.py`) can import `account_info_response`, `simulate_response`, `submit_response`, `mock_ws_connection`, and `atomic_opportunity` directly from `tests/conftest.py`
- **Plan 05-05** replay tests will use the same `mock_ws_connection` FIFO dispatch pattern to replay historical WS traffic
- The `_extract_intermediate` contract tested here (raises ValueError on XRP-only paths) is the same contract Plan 05-05 depends on for replay fixture validation

## Known Stubs

None. All 16 tests exercise real executor code paths. No hardcoded placeholder values in the test or fixture layer that would prevent tests from catching executor bugs.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes. The test file imports only from `src.*` and `tests.conftest`.

## Self-Check: PASSED

- `tests/conftest.py` — FOUND
- `tests/test_atomic_executor.py` — FOUND (657 lines)
- Commit 668302f — verified via `git log`
- Commit 3d24ec6 — verified via `git log`
- 154 tests pass (`pytest tests/ -x -q`)
- Test suite runtime: 0.29s for atomic tests alone (well under 2s target)
- `grep -c "def test_"` in test file: 16
- ATOM-01 split: both `test_both_legs_simulated_before_first_submit` and `test_both_legs_signed_before_first_submit` present
- Old name `test_atomic_both_legs_presigned_before_submit`: absent
