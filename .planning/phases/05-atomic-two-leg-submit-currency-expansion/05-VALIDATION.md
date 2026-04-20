---
phase: 5
slug: atomic-two-leg-submit-currency-expansion
status: ready
nyquist_compliant: true
wave_0_complete: false
created: 2026-04-20
updated_by_planner: 2026-04-20
---

# Phase 5 — Validation Strategy

> Per-phase validation contract. Populated by planner with the actual task-to-test map.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (already in use, 194 tests passing on branch) |
| **Config file** | `pytest.ini` (added by Plan 05-05 Task 2 if absent — markers-only scope, no asyncio_mode change) |
| **Quick run command** | `pytest tests/test_config.py tests/test_profit_math.py tests/test_simulator.py tests/test_executor.py tests/test_atomic_executor.py -x -q` |
| **Full suite command** | `pytest tests/ -x -q` |
| **Full suite without replay** | `pytest -m "not replay" tests/ -x -q` |
| **Replay only** | `pytest -m replay tests/ -x -q` |
| **Estimated runtime** | Quick: ~5s · Full: ~30s |

---

## Sampling Rate

- **After every task commit:** Run quick command
- **After every plan wave:** Run full suite
- **Before `/gsd-verify-work`:** Full suite must be green + replay tests green
- **Max feedback latency:** 30s

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Secure Behavior | Test Type | Automated Command | Status |
|---------|------|------|-------------|-----------------|-----------|-------------------|--------|
| 05-01 T1 | 05-01 | 1 | CLEAN-01 | LEG2_TIMEOUT_LEDGERS defined as int, default 4, documented in .env.example | unit | `pytest tests/test_config.py::test_leg2_timeout_ledgers_exists_and_defaults_to_four tests/test_config.py::test_env_example_documents_leg2_timeout_ledgers -x -q` | ⬜ pending |
| 05-01 T2 | 05-01 | 1 | CURR-01, CURR-02, CURR-03 | HIGH_LIQ default includes SOLO+USDT, every issuer documented, env reloads override default | unit | `pytest tests/test_config.py::test_high_liq_default_includes_solo_and_usdt tests/test_config.py::test_env_example_documents_every_high_liq_issuer tests/test_config.py::test_high_liq_env_override_reloads -x -q` | ⬜ pending |
| 05-01 T3 | 05-01 | 1 | CLEAN-02 | 3-tier threshold branching returns LOW_LIQ for non-HIGH_LIQ currencies | unit | `pytest tests/test_profit_math.py::test_get_profit_threshold_non_high_liq_returns_low_liq tests/test_profit_math.py::test_get_profit_threshold_low_liq_env_override -x -q` | ⬜ pending |
| 05-02 T1 | 05-02 | 1 | ATOM-07 | is_acceptable_sim_result + LEG2_ACCEPTABLE_CODES + strict leg-1 gate | unit | `pytest tests/test_simulator.py::test_leg2_acceptable_codes_is_frozenset_of_two_values tests/test_simulator.py::test_is_acceptable_sim_result_leg2_accepts_terpre_seq tests/test_simulator.py::test_is_acceptable_sim_result_leg1_strict -x -q` | ⬜ pending |
| 05-02 T2 | 05-02 | 1 | ATOM-07 | WS simulate returning terPRE_SEQ composes correctly with leg-2 helper | integration | `pytest tests/test_simulator.py::test_simulate_ws_terpre_seq_flows_through_to_leg2_helper -x -q` | ⬜ pending |
| 05-03 T1 | 05-03 | 2 | ATOM-09 | log_trade_leg + log_trade_summary helpers write additive JSONL entries (incl. path_used diagnostic) | unit | `pytest tests/test_trade_logger.py -x -q` | ⬜ pending |
| 05-03 T2 | 05-03 | 2 | ATOM-01..10 | Atomic executor rewrite — module imports cleanly, helpers present, no unused imports, path_used logged per leg | unit / structural | `python -c "from src.executor import TradeExecutor, _is_terminal_failure, _extract_intermediate; assert _is_terminal_failure('tecPATH_PARTIAL') is True"` + `pytest tests/test_executor.py -x -q` | ⬜ pending |
| 05-03 T3 | 05-03 | 2 | ATOM-10 | Existing public-contract tests still pass against rewritten executor | unit | `pytest tests/test_executor.py -x -q` | ⬜ pending |
| 05-04 T1 | 05-04 | 3 | (shared fixtures) | Pytest fixtures resolve without collection errors | collect | `pytest --collect-only tests/ -q` | ⬜ pending |
| 05-04 T2 | 05-04 | 3 | ATOM-01, 02, 03, 07 | Happy-path atomic flow + leg-2 sim gate semantics + signing-order assertion (ATOM-01 split into sim-order + sign-order tests per plan-checker Warning 3) | unit | `pytest tests/test_atomic_executor.py::test_both_legs_simulated_before_first_submit tests/test_atomic_executor.py::test_both_legs_signed_before_first_submit tests/test_atomic_executor.py::test_atomic_sequences_are_n_and_n_plus_1 tests/test_atomic_executor.py::test_atomic_leg2_submits_before_leg1_validates tests/test_atomic_executor.py::test_leg2_terPRE_SEQ_treated_as_pass -x -q` | ⬜ pending |
| 05-04 T3 | 05-04 | 3 | ATOM-04, 05, 06, 08, 09 | Failure paths, single-writer guard, Decimal preservation, per-leg logs | unit | `pytest tests/test_atomic_executor.py::test_leg1_terminal_fail_burns_sequence tests/test_atomic_executor.py::test_leg2_fail_activates_existing_recovery tests/test_atomic_executor.py::test_single_writer_guard_rejects_concurrent tests/test_atomic_executor.py::test_atomic_all_amounts_are_decimal tests/test_atomic_executor.py::test_atomic_per_leg_log_entries -x -q` | ⬜ pending |
| 05-05 T1 | 05-05 | 4 | (fixture setup) | Incident fixture files exist and are valid JSON | static | `python -c "import json; d = json.load(open('tests/fixtures/incident_2026_04_19/hashes.json')); assert len(d['hashes']) == 4"` | ⬜ pending |
| 05-05 T2 | 05-05 | 4 | ATOM-01..04, ATOM-07 (replay) | 4 incident hashes replay successfully under atomic flow; no drift window; replay marker registered (pytest.ini markers-only); 194-test baseline unaffected | integration | `pytest -m replay tests/test_replay_incident.py -x -q` | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Plan-checker revision notes (applied 2026-04-20):**
- Warning 1: Plan 05-05 now declares `depends_on: [05-03, 05-04]` — bumped to Wave 4 (was Wave 3).
- Warning 3: ATOM-01 split into `test_both_legs_simulated_before_first_submit` (sim ordering) + `test_both_legs_signed_before_first_submit` (sign ordering) — old over-promising name `test_atomic_both_legs_presigned_before_submit` retired. Both new tests are in Plan 05-04 Task 2.
- Info 2: ATOM-07 added to Plan 05-04 and Plan 05-05 `requirements` frontmatter (both tasks already tested terPRE_SEQ e2e, traceability cleanup).

---

## Wave 0 Requirements

All Wave 0 test-file creation is folded into the plans themselves:

- [x] `tests/test_config.py` — created by Plan 05-01 Task 1 (covers CLEAN-01, CURR-01, CURR-03)
- [x] `tests/test_atomic_executor.py` — created by Plan 05-04 Tasks 2 & 3 (covers ATOM-01 to ATOM-09)
- [x] `tests/test_replay_incident.py` — created by Plan 05-05 Task 2
- [x] `tests/fixtures/incident_2026_04_19/` — created by Plan 05-05 Task 1
- [x] `tests/conftest.py` — extended by Plan 05-04 Task 1 (shared fixtures)
- [x] `tests/test_simulator.py` — extended by Plan 05-02 (covers ATOM-07)
- [x] `tests/test_profit_math.py` — extended by Plan 05-01 (covers CLEAN-02)
- [x] `pytest.ini` — created by Plan 05-05 Task 2 (markers-only scope; registers `replay` and `slow` markers; no `asyncio_mode` change so the existing pytest-asyncio default behavior stays in effect for the 194-test baseline)
- [x] No framework install required — pytest + pytest-asyncio already in use

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| First live atomic trade on VPS | ATOM-10 | Must observe real-network behavior before declaring live-ready | After paper-mode burn-in, flip DRY_RUN=False on VPS, target a small MAX_TRADE_XRP_ABS (e.g., 0.5 XRP), wait for opportunity, verify both-leg hashes land in same or adjacent ledger in `xrpl_arb_log.jsonl` |
| Telegram alert routing for leg-1-fail vs leg-2-fail | ATOM-04, ATOM-05 | Requires live Telegram bot + simulated failure | Temporarily force leg-1 failure via blacklist injection, confirm Telegram alert clearly distinguishes "leg 1 failed — seq burned" from "leg 2 failed — recovery activated" |
| Issuer trustline verification for new HIGH_LIQ currencies | CURR-03 | Requires live mainnet trustline queries against documented issuer addresses | After `.env` update with new currencies, run bot in DRY_RUN=True and observe that `ripple_path_find` returns live paths for the new currencies (not 0-path) |
| Shared-paths leg-2 failure mode verification | ATOM-03 (post-deploy) | Can only be diagnosed from live trade data — `path_used` field in log_trade_leg entries tells us whether tecPATH_PARTIAL (if any) was an atomic-window issue vs a path-split issue | After 7-day paper burn-in + first live trades, inspect `xrpl_arb_log.jsonl`: if any leg-2 carries `engine_result=tecPATH_PARTIAL` with `latency_from_leg1_ms < 500`, that is a SIGNAL to escalate to per-leg path splitting in a future phase |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (test_atomic_executor.py, test_replay_incident.py, fixtures, pytest.ini)
- [x] No watch-mode flags (pytest with `-x -q` only)
- [x] Feedback latency < 30s
- [x] `nyquist_compliant: true` set in frontmatter after planner populates verification map
- [x] Plan-checker revisions applied (Warnings 1, 3, 5 + Info 1, 2, 3; Warning 2 on pytest.ini scope also applied)

**Approval:** ready for execution
