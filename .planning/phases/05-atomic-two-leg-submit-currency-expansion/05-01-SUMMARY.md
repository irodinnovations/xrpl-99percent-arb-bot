---
phase: 05-atomic-two-leg-submit-currency-expansion
plan: "01"
subsystem: config-profit-math
tags: [config, profit-math, currency-expansion, two-leg, tdd]
dependency_graph:
  requires: []
  provides:
    - LEG2_TIMEOUT_LEDGERS constant in src.config
    - HIGH_LIQ_CURRENCIES default expanded to 6 currencies
    - get_profit_threshold() 3-tier model (HIGH_LIQ / LOW_LIQ)
  affects:
    - src/config.py
    - src/profit_math.py
    - .env.example
    - tests/test_config.py
    - tests/test_profit_math.py
tech_stack:
  added: []
  patterns:
    - TDD (RED/GREEN for each task)
    - int(os.getenv(...)) pattern for XRPL ledger-index constants
    - 3-tier profit threshold model via get_profit_threshold()
key_files:
  created:
    - tests/test_config.py
  modified:
    - src/config.py
    - src/profit_math.py
    - .env.example
    - tests/test_profit_math.py
decisions:
  - PROFIT_THRESHOLD_LOW_LIQ is the non-HIGH_LIQ fallback; bare PROFIT_THRESHOLD is no longer in the get_profit_threshold() branch (it remains the default for is_profitable() override path only)
  - LEG2_TIMEOUT_LEDGERS typed as int, not Decimal — LastLedgerSequence is an XRPL integer field
  - HIGH_LIQ_CURRENCIES expansion to SOLO+USDT is env-only: restart required, no code path change needed (CURR-02)
  - Comments on their own lines in .env.example — python-dotenv chokes on inline comments
metrics:
  duration_minutes: 25
  completed_date: "2026-04-20"
  tasks_completed: 3
  files_modified: 5
---

# Phase 5 Plan 1: Config Foundations + Currency Expansion Summary

**One-liner:** LEG2_TIMEOUT_LEDGERS config knob + 3-tier get_profit_threshold() + SOLO/USDT expansion with documented issuer addresses.

## What Was Built

### Task 1: LEG2_TIMEOUT_LEDGERS (CLEAN-01)

Added `LEG2_TIMEOUT_LEDGERS: int = int(os.getenv("LEG2_TIMEOUT_LEDGERS", "4"))` to `src/config.py` immediately after the `HIGH_LIQ_CURRENCIES` block. The constant replaces the inline `+ 4` hardcode that Plan 05-03's atomic executor will consume. Typed as `int` (not `Decimal`) because `LastLedgerSequence` is an XRPL ledger-index integer, not a monetary value.

Documented in `.env.example` with a block comment explicitly mentioning "atomic two-leg" semantics so the test requiring that marker passes and future operators understand the purpose.

### Task 2: HIGH_LIQ_CURRENCIES Expansion (CURR-01, CURR-02, CURR-03)

Changed the `HIGH_LIQ_CURRENCIES` default in `src/config.py` from `"USD,USDC,RLUSD,EUR"` to `"USD,USDC,RLUSD,EUR,SOLO,USDT"`. Added a full `# --- HIGH_LIQ Currencies ---` section to `.env.example` documenting:
- The `HIGH_LIQ_CURRENCIES=USD,USDC,RLUSD,EUR,SOLO,USDT` key
- Trusted issuer r-addresses for all 6 currencies (USD/Bitstamp, USD/GateHub, USDC/Circle, RLUSD/Ripple, EUR/GateHub, SOLO/Sologenic, USDT/GateHub) sourced verbatim from `scripts/setup_trust_lines.py`
- `PROFIT_THRESHOLD_HIGH_LIQ` and `PROFIT_THRESHOLD_LOW_LIQ` documented entries

All issuer addresses are on separate comment lines — no inline comments (per python-dotenv constraint).

### Task 3: 3-Tier get_profit_threshold() (CLEAN-02)

Replaced the `return PROFIT_THRESHOLD` fallback in `src/profit_math.py` with `return PROFIT_THRESHOLD_LOW_LIQ`. The function now implements:
1. HIGH_LIQ currencies → `PROFIT_THRESHOLD_HIGH_LIQ` (0.003 default)
2. All other currencies → `PROFIT_THRESHOLD_LOW_LIQ` (0.010 default)

The base `PROFIT_THRESHOLD` (0.006) is no longer referenced in `get_profit_threshold()` — it remains the default for `is_profitable(threshold=...)` callers that don't go through `get_profit_threshold()`.

## Test Count Added

| Test file | Tests added | What they cover |
|-----------|-------------|-----------------|
| tests/test_config.py | 6 (new file) | LEG2_TIMEOUT_LEDGERS default+type, .env.example LEG2 doc, HIGH_LIQ includes SOLO/USDT, legacy 4 preserved, issuer addresses per currency, env override reload |
| tests/test_profit_math.py | 6 appended | HIGH_LIQ returns HIGH_LIQ, SOLO/USDT in HIGH_LIQ, non-HIGH_LIQ returns LOW_LIQ, case-insensitive, Decimal type, LOW_LIQ env override |

**Total new tests: 12** — full test suite: 126 passed (0 failures, 0 regressions).

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | dc0c922 | feat(05-01): add LEG2_TIMEOUT_LEDGERS to config + document in .env.example |
| 2 | 1623ed2 | feat(05-01): expand HIGH_LIQ_CURRENCIES + document issuer addresses in .env.example |
| 3 | b78126a | feat(05-01): wire PROFIT_THRESHOLD_LOW_LIQ into get_profit_threshold (3-tier model) |

## Deviations from Plan

None — plan executed exactly as written.

Task 1 and Task 2 both modified `src/config.py` and `tests/test_config.py`, which were committed as separate atomic commits (Task 1 created the file and added LEG2 knob; Task 2 committed the .env.example HIGH_LIQ section). The HIGH_LIQ default expansion in `src/config.py` was done in the Task 1 commit since the plan's `<action>` for Task 1 included that code change alongside LEG2_TIMEOUT_LEDGERS — this is not a deviation, the plan bundled both config.py changes in the same action block.

## Forward Dependencies

- **Plan 05-03** will `from src.config import LEG2_TIMEOUT_LEDGERS` — the constant is now available.
- **Plan 05-03** will not touch `get_profit_threshold()` — CLEAN-02 is independently locked by this plan's tests.
- **Plan 05-02** (simulator change, Wave 1 parallel) does not touch any files modified here.

## Known Stubs

None.

## Self-Check: PASSED

- `src/config.py` — LEG2_TIMEOUT_LEDGERS present, HIGH_LIQ default 6 currencies: FOUND
- `.env.example` — LEG2_TIMEOUT_LEDGERS=4, HIGH_LIQ_CURRENCIES=..., all 6 issuer addresses: FOUND
- `tests/test_config.py` — new file, 6 tests: FOUND
- `tests/test_profit_math.py` — 6 tests appended: FOUND
- `src/profit_math.py` — return PROFIT_THRESHOLD_LOW_LIQ in get_profit_threshold: FOUND
- Commits dc0c922, 1623ed2, b78126a: FOUND
- Full test suite 126 passed: VERIFIED
