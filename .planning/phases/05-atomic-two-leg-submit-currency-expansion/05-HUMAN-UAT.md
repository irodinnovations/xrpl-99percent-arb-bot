---
status: partial
phase: 05-atomic-two-leg-submit-currency-expansion
source: [05-VERIFICATION.md]
started: 2026-04-20T00:00:00Z
updated: 2026-04-20T00:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. First live atomic trade observation
expected: After paper-mode burn-in (DRY_RUN=False on VPS with MAX_TRADE_XRP_ABS=0.5), `xrpl_arb_log.jsonl` shows two `entry_type: leg` entries (leg 1 + leg 2) with `latency_from_leg1_ms` under 500ms, followed by `entry_type: summary` with `outcome: both_legs_success`.
result: [pending]

### 2. Telegram alert routing distinguishes leg-1 vs leg-2 failure
expected: LEG 1 FAILED alert contains "Sequence N+1 burn: OK/FAILED"; LEG 2 FAILED alert contains "2% recovery engaged".
result: [pending]

### 3. SOLO and USDT trust-line path availability
expected: Bot log shows DRY-RUN (atomic) entries for SOLO or USDT opportunities, confirming `ripple_path_find` routes through those issuers in live DEX liquidity.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
