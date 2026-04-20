---
phase: 05-atomic-two-leg-submit-currency-expansion
plan: "05"
subsystem: test-replay-incident
tags: [tests, replay, atomic-submit, two-leg, ATOM-01, ATOM-02, ATOM-03, ATOM-04, ATOM-07, incident-2026-04-19]
dependency_graph:
  requires:
    - "05-03 provides TradeExecutor (atomic two-leg submit)"
    - "05-04 provides tests/conftest.py shared fixtures (mock_ws_connection, mock_wallet, account_info_response, simulate_response, submit_response)"
  provides:
    - "tests/test_replay_incident.py — 9 replay tests: 4 happy-path + 4 terPRE_SEQ + 1 drift-window"
    - "tests/fixtures/incident_2026_04_19/hashes.json — 4 incident trade hashes + opportunity shapes"
    - "tests/fixtures/incident_2026_04_19/README.md — fixture capture documentation"
    - "pytest.ini — markers-only config (replay + slow markers; no asyncio_mode change)"
  affects:
    - tests/test_replay_incident.py
    - tests/fixtures/incident_2026_04_19/hashes.json
    - tests/fixtures/incident_2026_04_19/README.md
    - pytest.ini
tech_stack:
  added: []
  patterns:
    - Parameterized pytest replay against fixture data (no live network)
    - FIFO mock WS response dispatch pattern (reused from conftest)
    - @pytest.mark.replay for CI-selective test skipping
    - pytest.ini markers-only scope to preserve existing asyncio behavior
key_files:
  created:
    - tests/test_replay_incident.py
    - tests/fixtures/incident_2026_04_19/hashes.json
    - tests/fixtures/incident_2026_04_19/README.md
    - pytest.ini
  modified: []
decisions:
  - "Fixture approach rather than live RPC historical queries: mainnet simulate RPC has no ledger_index parameter (per RESEARCH Open Question #2); fixture values are approximate but sufficient to prove the TIMING fix"
  - "pytest.ini scoped to markers-only — no asyncio_mode change — to preserve 154-test baseline using explicit @pytest.mark.asyncio decorators"
  - "@pytest.mark.replay gating allows CI to skip replay tests with -m 'not replay' for fast iteration"
  - "test_replay_incident_no_drift_window_between_legs asserts the core architectural invariant: no tx/submit_and_wait between the two submit calls"
metrics:
  duration_minutes: 8
  completed_date: "2026-04-20"
  tasks_completed: 2
  files_modified: 4
---

# Phase 5 Plan 5: Incident Replay Harness Summary

**One-liner:** 9-test parameterized replay harness proves the 2026-04-19 drift-window fix — atomic submit passes both sim gates for all 4 incident hashes with no tx/submit_and_wait between legs.

## What Was Built

### Task 1: Incident fixture files

Created `tests/fixtures/incident_2026_04_19/` with:

- **`hashes.json`** — manifest of 4 failed live trade hashes (2EBD65E8, E8A24309, 1C63E5763115D09F, D6B62B3121F56901) with per-hash approximate opportunity shapes (input/output XRP, profit_pct, profit_ratio) and GateHub USD issuer address. Importable by pathlib without runtime dependencies.
- **`README.md`** — documents the fixture-capture rationale (why live historical RPC was not used), includes the `s2.ripple.com book_offers` pattern for future incident capture.

### Task 2: Parameterized replay test harness + pytest.ini

Created `tests/test_replay_incident.py` with 3 test functions covering 9 test cases:

| Test | Parametrized | Cases | REQ | What it asserts |
|------|-------------|-------|-----|-----------------|
| `test_replay_incident_atomic_passes_both_sim_gates` | Yes (4 hashes) | 4 | ATOM-01..04 | executor.execute() returns True; outcome=both_legs_success |
| `test_replay_incident_leg2_terPRE_SEQ_boundary` | Yes (4 hashes) | 4 | ATOM-07 | terPRE_SEQ on leg 2 sim accepted; executor returns True |
| `test_replay_incident_no_drift_window_between_legs` | No | 1 | ATOM-03 | No `tx`/`submit_and_wait`/`ledger` commands between the two submits |

Created `pytest.ini` at project root with markers-only scope (registers `replay` and `slow` markers; no `asyncio_mode` line so the existing pytest-asyncio default behavior is preserved for the 154-test baseline).

## Test Count Summary

| File | Tests added | Runtime |
|------|-------------|---------|
| tests/test_replay_incident.py | 9 | 0.25s |
| pytest.ini | 0 (config only) | — |

**Total tests: 163 passed (154 original + 9 new replay tests, 0 failures, 0 regressions)**

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 021d84c | feat(05-05): add incident fixture files for 2026-04-19 replay harness |
| 2 | eca7c23 | test(05-05): add parameterized replay harness for 2026-04-19 incident hashes |

## Deviations from Plan

None — plan executed exactly as written. The test file matches the plan's action template verbatim. pytest.ini content matches the plan's specified content exactly.

## Known Stubs

None. The replay harness exercises real executor code paths via the same mock_ws_connection FIFO pattern as test_atomic_executor.py. Fixture opportunity values are approximate (documented in README.md and plan threat register T-05-16) — the architectural assertion (`test_replay_incident_no_drift_window_between_legs`) does not depend on exact numeric values.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes introduced. The test and fixture files import only from `src.*`, `tests.conftest`, and stdlib.

## Phase 5 Completion

Plan 05-05 is the final plan in Phase 5. With this summary committed:
- Wave 1 (05-01, 05-02): config + simulator gates ✅
- Wave 2 (05-03): atomic executor rewrite ✅
- Wave 3 (05-04): atomic executor test suite ✅
- Wave 4 (05-05): incident replay harness ✅

Phase 5 is ready for `/gsd-verify-work`.

## Self-Check: PASSED

- `tests/test_replay_incident.py` — FOUND
- `tests/fixtures/incident_2026_04_19/hashes.json` — FOUND
- `tests/fixtures/incident_2026_04_19/README.md` — FOUND
- `pytest.ini` — FOUND
- Commit 021d84c — verified via git log
- Commit eca7c23 — verified via git log
- 9 replay tests pass (`python -m pytest tests/test_replay_incident.py -x -q`)
- 163 total tests pass (`python -m pytest tests/ -x -q`)
- `grep -n "asyncio_mode" pytest.ini` returns no match (correct)
- `pytest -m "not replay" tests/test_replay_incident.py --co -q` exits 5 (no tests collected)
- `pytest --strict-markers tests/test_replay_incident.py -x -q` exits 0
