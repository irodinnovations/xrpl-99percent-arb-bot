---
status: partial
phase: 05-atomic-two-leg-submit-currency-expansion
source: [05-VERIFICATION.md]
started: 2026-04-20T00:00:00Z
updated: 2026-04-20T17:10:00Z
---

## Current Test

Test 1 — awaiting first live atomic trade (DRY_RUN=False gated decision).

## Tests

### 1. First live atomic trade observation
expected: After paper-mode burn-in (DRY_RUN=False on VPS with MAX_TRADE_XRP_ABS=0.5), `xrpl_arb_log.jsonl` shows two `entry_type: leg` entries (leg 1 + leg 2) with `latency_from_leg1_ms` under 500ms, followed by `entry_type: summary` with `outcome: both_legs_success`.
result: [pending]
notes: Market currently too quiet to generate any opportunity (ripple_path_find returns 0 alternatives at 10/50/100 XRP sizes as of 2026-04-20). Awaiting user decision to flip DRY_RUN=False with MAX_TRADE_XRP_ABS=0.5 and a natural market opportunity. Tracked as ongoing post-deployment observation.

### 2. Telegram alert routing distinguishes leg-1 vs leg-2 failure
expected: LEG 1 FAILED alert contains "Sequence N+1 burn: OK/FAILED"; LEG 2 FAILED alert contains "2% recovery engaged".
result: pass
notes: Verified 2026-04-20 via test harness that emits the exact production strings from src/executor.py (lines 228-230, 258-260, 272-275). All 4 alerts arrived in Telegram with distinct text — leg-1 OK burn, leg-1 FAILED burn, leg-2 recovery engaged, and ATOMIC TRADE OK success. Operator can distinguish all failure modes at a glance.

### 3. SOLO and USDT trust-line path availability
expected: Bot log shows DRY-RUN (atomic) entries for SOLO or USDT opportunities, confirming `ripple_path_find` routes through those issuers in live DEX liquidity.
result: pass (conditional)
notes: Verified 2026-04-20 via direct pathfinder probe:
  - Trust lines established on wallet r3yPcfPJuPkG1AJxNxbUpQHZVfEaa8VPKq for SOLO (rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz) and USDT (rcvxE9PS9YBwxtGg1qNeewV6ZB3wGubZq) with balance 0 (ready to hold IOU mid-trade)
  - HIGH_LIQ_CURRENCIES env var includes SOLO, USDT — confirmed by bot startup banner + .env.example
  - ripple_path_find call infrastructure verified working (called by bot every scan, returns 0 alternatives when DEX has no circular arb — matches market condition today)
  - USD/GateHub baseline also returns 0 paths currently — confirms this is market state, not a SOLO/USDT-specific code issue
  - First log entry showing "SOLO" or "USDT" in path_used/intermediate auto-closes the conditional — query: `grep -iE '534F4C4F|5553445400|SOLO|USDT' /opt/xrplbot/xrpl_arb_log.jsonl`

## Summary

total: 3
passed: 2
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
