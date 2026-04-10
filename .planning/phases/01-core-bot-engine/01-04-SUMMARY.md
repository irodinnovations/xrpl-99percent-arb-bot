---
phase: 01-core-bot-engine
plan: "04"
subsystem: safety
tags: [circuit-breaker, blacklist, decimal, safety, tdd]
dependency_graph:
  requires: ["01-01"]
  provides: ["CircuitBreaker", "Blacklist"]
  affects: ["src/safety.py", "tests/test_safety.py"]
tech_stack:
  added: []
  patterns: ["TDD red-green-refactor", "timezone-aware datetime", "Decimal-only financial math"]
key_files:
  created:
    - src/safety.py
    - tests/test_safety.py
  modified: []
decisions:
  - "datetime.now(timezone.utc) used instead of deprecated datetime.utcnow() for Python 3.14 compatibility"
  - "_utcnow() helper function centralizes timezone-aware UTC time to avoid repeating datetime.now(timezone.utc)"
  - "CircuitBreaker halt check is separate from record_trade — is_halted() must be called explicitly by the scanner loop"
metrics:
  duration: "3m"
  completed: "2026-04-10"
  tasks: 1
  files: 2
---

# Phase 01 Plan 04: Safety Systems Summary

**One-liner:** CircuitBreaker with 24h halt at 2% daily loss + Blacklist for path/token filtering, all Decimal-only math.

## What Was Built

`src/safety.py` delivers two safety guardrails:

**CircuitBreaker** — tracks cumulative daily P&L in XRP using `Decimal`. When losses reach `DAILY_LOSS_LIMIT_PCT` (2%) of `reference_balance`, sets `_halt_until` 24 hours in the future. `is_halted()` returns `True` until that time expires, at which point state resets automatically. Gains offset losses (net tracking). Optionally refreshes reference balance from the live ledger via `update_reference_balance()`.

**Blacklist** — maintains sets of blacklisted currency codes and issuer addresses. `is_blacklisted(paths)` iterates path steps from `ripple_path_find` responses and returns `True` on any match. Case-insensitive currency matching. Empty blacklist short-circuits to `False` (no overhead when unused).

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for CircuitBreaker + Blacklist | 3eca063 | tests/test_safety.py |
| 1 (GREEN+REFACTOR) | Implement safety module, fix utcnow deprecation | 5d38718 | src/safety.py, tests/test_safety.py |

## Verification Results

```
14 passed in 0.16s  (zero warnings)
```

- `grep -c "float" src/safety.py` → 1 (the word appears only in the docstring comment "no float" — zero actual float usage)
- `grep "SAFE-02|SAFE-03|SAFE-04"` → all three present
- `python -c "from src.safety import CircuitBreaker, Blacklist; print('imports OK')"` → imports OK

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Replaced deprecated datetime.utcnow() with timezone-aware datetime**
- **Found during:** REFACTOR phase — Python 3.14 emitted DeprecationWarning on every test
- **Issue:** `datetime.utcnow()` is deprecated in Python 3.12+ and scheduled for removal; produces naive datetime objects
- **Fix:** Added `from datetime import timezone`, introduced `_utcnow()` helper returning `datetime.now(timezone.utc)`, replaced all 4 call sites in safety.py and 1 in test_safety.py
- **Files modified:** src/safety.py, tests/test_safety.py
- **Commit:** 5d38718

## Known Stubs

None — CircuitBreaker and Blacklist are fully functional. `update_reference_balance()` requires a live connection but is not called by tests (correct — it's an async method for runtime use).

## Threat Flags

No new threat surface introduced beyond what was documented in the plan's threat model.

| Threat ID | Mitigation Status |
|-----------|------------------|
| T-01-13 | Mitigated — CircuitBreaker uses in-memory state; halt_until uses timezone-aware datetime comparison |
| T-01-14 | Mitigated — XRPL_SECRET never referenced or logged in safety.py |
| T-01-16 | Mitigated — DRY_RUN defaults to True in config.py (verified from existing config.py) |

## Self-Check: PASSED

- `src/safety.py` exists: FOUND
- `tests/test_safety.py` exists: FOUND
- Commit 3eca063 (RED): FOUND
- Commit 5d38718 (GREEN+REFACTOR): FOUND
- 14 tests pass: VERIFIED
