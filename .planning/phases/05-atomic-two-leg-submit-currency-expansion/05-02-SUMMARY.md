---
phase: 05-atomic-two-leg-submit-currency-expansion
plan: "02"
subsystem: simulator
tags: [atomic-submit, leg-2, terPRE_SEQ, simulate, ATOM-07]
dependency_graph:
  requires: []
  provides: [is_acceptable_sim_result, LEG2_ACCEPTABLE_CODES]
  affects: [src/executor.py (Plan 05-03 consumes is_acceptable_sim_result for leg-2 gate)]
tech_stack:
  added: []
  patterns: [keyword-only parameter for safety, frozenset for immutable whitelist]
key_files:
  created: []
  modified:
    - src/simulator.py
    - tests/test_simulator.py
decisions:
  - "Additive-only change — no existing simulate helper modified — keeps leg-1 and all legacy callers on strict tesSUCCESS"
  - "is_leg_2 is keyword-only (*, is_leg_2) — positional call raises TypeError at callsite (T-05-07)"
  - "LEG2_ACCEPTABLE_CODES is a frozenset — immutable at runtime (T-05-06)"
  - "SimResult.success remains anchored to exact tesSUCCESS; helper is a separate post-result gate"
metrics:
  duration: "~3 minutes"
  completed: "2026-04-20"
  tasks_completed: 2
  files_modified: 2
---

# Phase 05 Plan 02: Leg-2 Simulate Acceptance Helper Summary

**One-liner:** Additive `is_acceptable_sim_result(result_code, *, is_leg_2)` helper + `LEG2_ACCEPTABLE_CODES = frozenset({"tesSUCCESS", "terPRE_SEQ"})` constant in `src/simulator.py` — backward-compatible leg-2 gate for atomic executor (Plan 05-03).

## What Was Built

### src/simulator.py

Added after the `SimResult` dataclass definition (before `RpcClientProtocol`):

- **`LEG2_ACCEPTABLE_CODES: frozenset[str]`** — immutable module-level constant containing exactly `{"tesSUCCESS", "terPRE_SEQ"}`. The `terPRE_SEQ` code is the authoritative XRPL signal that a transaction with `Sequence = N+1` is valid but cannot apply until the account's current Sequence `N` advances — the expected state when leg 2 is simulated against pre-leg-1 account state.

- **`is_acceptable_sim_result(result_code: str, *, is_leg_2: bool) -> bool`** — keyword-only whitelist check. When `is_leg_2=False` (leg 1 or any legacy caller), requires exact `tesSUCCESS`. When `is_leg_2=True` (atomic executor's leg-2 simulate gate), accepts any code in `LEG2_ACCEPTABLE_CODES`.

The existing `simulate_transaction`, `simulate_transaction_ws`, and `SimResult.success` were **not modified**. They continue to return `success=True` only on exact `tesSUCCESS`.

### tests/test_simulator.py

Added 9 new tests across two groups:

**Task 1 — helper unit tests (7 tests):**
- `test_leg2_acceptable_codes_is_frozenset_of_two_values` — asserts exact frozenset contents
- `test_is_acceptable_sim_result_leg1_strict` — leg-1 mode rejects terPRE_SEQ + other non-SUCCESS codes
- `test_is_acceptable_sim_result_leg2_accepts_terpre_seq` — leg-2 mode accepts tesSUCCESS AND terPRE_SEQ
- `test_is_acceptable_sim_result_leg2_rejects_terminal_failures` — 7 terminal codes all rejected by leg-2 gate
- `test_existing_simulate_helpers_unchanged_still_strict` — regression guard: SimResult.success stays False on terPRE_SEQ even though helper now accepts it

**Task 2 — WS simulate integration tests (2 tests):**
- `test_simulate_ws_terpre_seq_flows_through_to_leg2_helper` — WS simulate returns terPRE_SEQ → SimResult.success=False (strict) → helper accepts for leg-2; no HTTP fallback triggered
- `test_simulate_ws_tessuccess_accepted_by_both_helpers` — happy path: tesSUCCESS accepted by both is_leg_2=True and is_leg_2=False

## New Exports for Plan 05-03

Plan 05-03's `AtomicExecutor` will consume both symbols:

```python
from src.simulator import is_acceptable_sim_result, LEG2_ACCEPTABLE_CODES

# In atomic execute, after sim2 = await simulate_transaction_ws(leg2_tx_dict, connection):
if not is_acceptable_sim_result(sim2.result_code, is_leg_2=True):
    return False  # leg-2 sim failed — abort before submitting leg 1
```

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| Additive-only — no existing helpers modified | Keeps all 121 existing tests green; zero risk to the current single-leg execution path in production |
| `is_leg_2` keyword-only parameter | Prevents positional misuse (e.g., `is_acceptable_sim_result(code, True)`) — any wrong-order call raises TypeError at import/callsite (T-05-07 mitigation) |
| `LEG2_ACCEPTABLE_CODES` as `frozenset` | Immutable at runtime — cannot be mutated by rogue code path or accidental `add()` call (T-05-06 mitigation) |
| `SimResult.success` unchanged | All non-atomic callers (leg 1, backtester, AI brain) stay strict without any code changes; atomic executor adds its own gate on top |
| Plan 05-02 runs in parallel with Plan 05-01 | No shared files — 05-01 touches `src/profit_math.py` and `src/config.py`; 05-02 touches `src/simulator.py` and `tests/test_simulator.py` |

## Test Results

```
121 passed in 2.11s
```

All new tests green. All 5 pre-existing simulator tests still pass (regression-free).

## Deviations from Plan

None — plan executed exactly as written. The TDD flow (RED → GREEN) was followed: tests written and confirmed failing on ImportError before implementation was added.

## Known Stubs

None. The helper is fully implemented with real logic; no placeholder return values or TODO markers.

## Self-Check: PASSED

- `src/simulator.py` — FOUND, contains `LEG2_ACCEPTABLE_CODES` and `is_acceptable_sim_result`
- `tests/test_simulator.py` — FOUND, contains all 7 + 2 new tests
- Commit `b2c9add` — FOUND in git log
- All 121 tests pass
