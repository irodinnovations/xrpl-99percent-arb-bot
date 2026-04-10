---
phase: 01-core-bot-engine
plan: 02
subsystem: pathfinder + profit-math
tags: [pathfinding, profit-math, decimal, tdd, xrpl]
dependency_graph:
  requires: ["01-01"]
  provides: ["src/pathfinder.py", "src/profit_math.py"]
  affects: ["01-03", "01-04"]
tech_stack:
  added: []
  patterns: ["TDD red-green", "pure-function math module", "dataclass for structured data", "Decimal(str()) parsing at trust boundaries"]
key_files:
  created:
    - src/profit_math.py
    - src/pathfinder.py
    - tests/test_profit_math.py
    - tests/test_pathfinder.py
  modified: []
decisions:
  - "DROPS_PER_XRP constant set to Decimal('1000000') — keeps all drop arithmetic in Decimal domain"
  - "parse_alternatives skips dict source_amounts — XRP-only strategy, non-XRP sources deferred"
  - "Decimal(str(...)) used for all XRPL node amounts — prevents silent float contamination at trust boundary (T-01-05)"
  - "is_profitable uses strictly-greater-than — equals threshold is NOT profitable"
metrics:
  duration: "3 minutes"
  completed_date: "2026-04-10"
  tasks_completed: 2
  files_created: 4
  files_modified: 0
---

# Phase 01 Plan 02: Pathfinder and Profit Math Summary

**One-liner:** Decimal-only profit math with dynamic slippage and PathFinder class that scans for arbitrage via ripple_path_find with strict profit filtering.

## What Was Built

**`src/profit_math.py`** — Pure function math module with zero side effects:
- `calculate_slippage(volatility_factor)`: base 0.003 + (0.001 * volatility) — dynamic buffer
- `calculate_profit(input_xrp, output_xrp, volatility_factor)`: net ratio after network fee and slippage
- `is_profitable(...)`: strictly-greater-than PROFIT_THRESHOLD check (0.006)
- `calculate_position_size(balance)`: 5% of account balance as Decimal

**`src/pathfinder.py`** — Scanning brain for arbitrage:
- `Opportunity` dataclass: `input_xrp`, `output_xrp`, `profit_pct`, `profit_ratio`, `paths`, `source_currency`
- `PathFinder.build_path_request(input_xrp)`: builds `RipplePathFind` for XRP-to-XRP loop
- `PathFinder.parse_alternatives(response, input_xrp)`: filters to only profitable opportunities, skips malformed
- `PathFinder.scan(account_balance)`: async full cycle — size -> request -> parse

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| Decimal(str(amount)) at XRPL trust boundary | XRPL amounts arrive as strings; wrapping in str() first prevents any hidden float conversion |
| Skip dict source_amounts | Non-XRP intermediate currencies are out of scope for Phase 1 XRP-only strategy |
| is_profitable uses `>` not `>=` | Exact-threshold trades have zero margin; strictly greater than enforces safety |
| DROPS_PER_XRP as module-level Decimal constant | Shared across build_path_request and parse_alternatives; single source of truth |

## Test Coverage

| File | Tests | Result |
|------|-------|--------|
| tests/test_profit_math.py | 9 | All PASS |
| tests/test_pathfinder.py | 6 | All PASS |
| **Total** | **15** | **15/15 PASS** |

## Commits

| Hash | Message |
|------|---------|
| `ee6968e` | test(01-02): add failing tests for profit math module |
| `cca1d26` | feat(01-02): implement Decimal profit math module |
| `ed15300` | test(01-02): add failing tests for PathFinder class |
| `1a836bf` | feat(01-02): implement PathFinder class with ripple_path_find integration |

## Deviations from Plan

None — plan executed exactly as written. Threat mitigations T-01-05 and T-01-06 were applied as coded: Decimal(str()) parsing at trust boundary and strictly-greater-than threshold comparison.

## Known Stubs

None. All functions are fully implemented and return real computed values.

## Threat Flags

None. All threat model mitigations from T-01-05 and T-01-06 are implemented:
- T-01-05: All XRPL response amounts parsed through `Decimal(str(...))` with try/except — malformed alternatives skipped
- T-01-06: Strict Decimal-only math, no float conversion, `profit > PROFIT_THRESHOLD` (not >=)

## Self-Check: PASSED

- [x] src/profit_math.py exists
- [x] src/pathfinder.py exists
- [x] tests/test_profit_math.py exists (9 tests, all pass)
- [x] tests/test_pathfinder.py exists (6 tests, all pass)
- [x] Commits ee6968e, cca1d26, ed15300, 1a836bf all exist
- [x] No float() conversions in either source file
- [x] Imports verified: `python -c "from src.pathfinder import PathFinder, Opportunity; print('imports OK')"`
