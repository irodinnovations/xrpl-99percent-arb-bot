---
phase: 01-core-bot-engine
plan: "03"
subsystem: simulation-gate-executor-entrypoint
tags: [simulator, executor, main, dry-run, live-trading, tdd]
dependency_graph:
  requires: ["01-01", "01-02", "01-04", "01-05"]
  provides: [simulate_transaction, TradeExecutor, main_entry_point]
  affects: [all-modules]
tech_stack:
  added:
    - HttpRpcClient (custom thin HTTP wrapper for XRPL JSON-RPC simulate calls)
  patterns:
    - TDD red-green cycle for all new modules
    - Raw tx_dict construction bypassing xrpl-py model validation for cross-currency paths
    - asyncio.to_thread for blocking HTTP calls in async context
    - Protocol-based duck typing for injectable RPC clients in tests
key_files:
  created:
    - src/simulator.py
    - src/executor.py
    - main.py
    - tests/test_simulator.py
    - tests/test_executor.py
  modified: []
decisions:
  - "HttpRpcClient used for simulate calls instead of xrpl-py JsonRpcClient — xrpl-py model validation rejects cross-currency Payment tx dicts before they reach the network"
  - "Raw tx_dict built directly in executor instead of Payment model — xrpl-py disallows same-account XRP-to-XRP with paths, but XRPL network allows cross-currency IOU-routed payments"
  - "Live execution uses sign RPC method (server-side sign+autofill) then submit — avoids xrpl-py autofill/sign model constraints while keeping the same security model"
  - "TF_PARTIAL_PAYMENT flag (131072) required on XRP-loop path payments where both amount and send_max are XRP"
metrics:
  duration: "~20m"
  completed: "2026-04-10"
  tasks_completed: 2
  files_created: 5
---

# Phase 01 Plan 03: Simulation Gate, Executor, and Main Entry Point Summary

**One-liner:** tesSUCCESS-gated trade executor with DRY_RUN branching wired into a single async bot loop via main.py

## What Was Built

### Task 1: Simulator and Executor (TDD)

**`src/simulator.py`** — simulate RPC gate
- `SimResult` dataclass: `success`, `result_code`, `raw`, `error`
- `HttpRpcClient`: thin HTTP wrapper for XRPL JSON-RPC calls (replaces xrpl-py `JsonRpcClient` for simulate, which has model validation constraints)
- `simulate_transaction(tx_dict, rpc_client)`: POSTs to XRPL simulate endpoint, only returns `success=True` on exact `"tesSUCCESS"` match in `meta.TransactionResult` (T-01-08)

**`src/executor.py`** — trade executor
- `TradeExecutor.execute(opportunity)`: gates on circuit breaker + blacklist + simulation before any trade path
- DRY_RUN=True: logs and sends Telegram alert, no network submission
- DRY_RUN=False: uses `sign` RPC (server-side autofill+sign) then `submit` blob; records engine_result; triggers circuit breaker on success
- Failed live submissions logged with full result dict (LIVE-03)

### Task 2: main.py

Single entry point wiring all modules:
- Validates `XRPL_SECRET` before connecting
- Initializes `XRPLConnection`, `PathFinder`, `CircuitBreaker`, `Blacklist`, `TradeExecutor`
- Registers `on_ledger_close` callback that scans every ~3-5s
- Top-level `try/except` in callback prevents any scan error from crashing the bot (T-01-11)
- Heartbeat log every ~50 ledgers; startup Telegram alert
- `asyncio.run(main())` as single CLI entry point

## Tests

| Test | Result |
|------|--------|
| `test_simulate_success` | PASS |
| `test_simulate_tec_path_dry` | PASS |
| `test_simulate_exception` | PASS |
| `test_dry_run_logs_without_submit` | PASS |
| `test_simulation_failure_skips` | PASS |
| `test_circuit_breaker_halted_skips` | PASS |
| All prior tests (45) | PASS |

**Total: 51/51 tests pass**

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] xrpl-py `Request` class does not accept `command` kwarg**
- **Found during:** Task 1 GREEN phase
- **Issue:** The plan's skeleton used `Request(command="simulate", tx_json=tx_dict)` but xrpl-py's `Request.__init__` only accepts `method: RequestMethod` — no `command` or `tx_json` fields
- **Fix:** Created `HttpRpcClient` using `requests.post()` directly to POST JSON-RPC payloads, bypassing xrpl-py model constraints entirely for the simulate call
- **Files modified:** `src/simulator.py`
- **Commit:** 273bb6c

**2. [Rule 1 - Bug] xrpl-py `Payment` model rejects XRP-to-XRP path payments**
- **Found during:** Task 1 GREEN phase
- **Issue:** `Payment(account=addr, amount=drops, destination=addr, paths=paths, send_max=drops)` raises `XRPLModelException` with "An XRP-to-XRP payment cannot contain paths" and "same sender and destination" errors. xrpl-py's model enforces rules that are more restrictive than the XRPL network itself (which allows cross-currency routing via IOU hops)
- **Fix:** Built `tx_dict` as a raw Python dict with `TransactionType`, `Amount`, `Destination`, `Paths`, `SendMax`, and `Flags: TF_PARTIAL_PAYMENT`. This matches what the pathfinder returns and what the simulate/submit RPCs accept
- **Files modified:** `src/executor.py`
- **Commit:** 273bb6c

**3. [Rule 1 - Bug] Live execution path redesigned to use `sign` RPC instead of xrpl-py autofill+sign**
- **Found during:** Task 1 implementation
- **Issue:** `xrpl.transaction.autofill` and `sign` take `Transaction` model objects, not raw dicts; would require reverting to model-based construction which fails for the arb payment pattern
- **Fix:** Live path uses XRPL's `sign` RPC method (server-side autofill+sign) returning `tx_blob`, then `submit` with the blob. Security model is equivalent — wallet seed is used server-side only on a trusted self-hosted node
- **Files modified:** `src/executor.py`
- **Commit:** 273bb6c

## Threat Model Coverage

All T-01-08 through T-01-12 mitigations implemented:

| Threat | Mitigation | Location |
|--------|------------|----------|
| T-01-08 Tampering — simulate response | Exact `"tesSUCCESS"` string match; any other value rejects | `simulator.py:68` |
| T-01-09 Repudiation — trade execution | Every trade (paper+live) logged to JSONL with timestamp, amounts, hash, result | `executor.py:trade_data` |
| T-01-10 Info Disclosure — wallet seed | Seed from `.env` only; no logging; process-level isolation enforced by main.py guard | `main.py:44` |
| T-01-11 DoS — main loop crash | `try/except` in `on_ledger_close`; connection auto-reconnects independently | `main.py:82` |
| T-01-12 EoP — DRY_RUN bypass | `DRY_RUN=True` default in config.py; requires explicit `.env` change | `config.py`, `executor.py` |

## Commits

| Hash | Message |
|------|---------|
| b3998a6 | test(01-03): add failing tests for simulator and executor |
| 273bb6c | feat(01-03): implement simulator and executor with simulation gate |
| ccb65cb | feat(01-03): add main.py entry point wiring all bot modules |

## Self-Check: PASSED

- `src/simulator.py` — exists, contains `tesSUCCESS`, `SimResult`, `simulate_transaction`
- `src/executor.py` — exists, contains `TradeExecutor`, `dry_run`, circuit breaker gate, simulation gate
- `main.py` — exists, valid syntax, imports resolve
- `tests/test_simulator.py` + `tests/test_executor.py` — 6/6 tests pass
- All 51 tests pass
