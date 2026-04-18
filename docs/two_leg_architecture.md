# Two-Leg Arbitrage Architecture

## Context

The previous execution model assumed a single `Payment` transaction could atomically round-trip XRP → IOU → XRP through path routing. **This assumption is invalid under the XRPL protocol.**

Rippled source ([Payment.cpp:153-212](https://github.com/XRPLF/rippled/blob/develop/src/libxrpl/tx/transactors/payment/Payment.cpp#L153)) rejects any payment where `xrpDirect = srcAsset.native() && dstAsset.native()` with:

- `SendMax` present → `temBAD_SEND_XRP_MAX`
- `Paths` present → `temBAD_SEND_XRP_PATHS`
- `tfPartialPayment` flag → `temBAD_SEND_XRP_PARTIAL`

The new model executes arbitrage as **two sequential Payment transactions**, each of which is protocol-legal because at least one side is an IOU.

## Transaction shapes

### Leg 1: Buy IOU with XRP

```json
{
  "TransactionType": "Payment",
  "Account": "rWallet",
  "Destination": "rWallet",
  "Amount": {"currency": "USD", "issuer": "rCheapIssuer", "value": "X"},
  "SendMax": "Y_drops",
  "Sequence": N,
  "Fee": "12",
  "LastLedgerSequence": currentLedger + 4
}
```

`xrpDirect = false` (dst is IOU), so this is legal. No `Paths` needed — rippled's default pathfinding walks the XRP/USD book for the specific `rCheapIssuer`.

### Leg 2: Sell IOU for XRP

```json
{
  "TransactionType": "Payment",
  "Account": "rWallet",
  "Destination": "rWallet",
  "Amount": "Z_drops",
  "SendMax": {"currency": "USD", "issuer": "rHeldIssuer", "value": "X"},
  "Paths": [[{"currency": "USD", "issuer": "rRichIssuer"}]],
  "Sequence": N+1,
  "Fee": "12",
  "LastLedgerSequence": currentLedger + 4
}
```

`xrpDirect = false` (src is IOU), so legal. `Paths` explicitly routes through `rRichIssuer`'s book to capture the cross-issuer spread.

Same-issuer arbitrage omits `Paths` — just sells the held IOU on its native book at the rich bid.

## Execution sequence

```
   ┌─────────────────┐
   │ Opportunity     │
   │ detected        │
   └────────┬────────┘
            ▼
   ┌─────────────────┐      reject
   │ Pre-sim leg 1   │───fail──→ abort
   └────────┬────────┘
            ▼ tesSUCCESS
   ┌─────────────────┐      reject
   │ Pre-sim leg 2   │───fail──→ abort
   │ (Seq = N+1)     │
   └────────┬────────┘
            ▼ tesSUCCESS
   ┌─────────────────┐
   │ DRY_RUN?        │──yes──→ log both, exit
   └────────┬────────┘
            ▼ no
   ┌─────────────────┐      network fail
   │ Submit leg 1    │───────────→ abort (no state)
   └────────┬────────┘
            ▼
   ┌─────────────────┐      tx failed
   │ Wait validated  │───────────→ abort (no state)
   │ (max 4 ledgers) │
   └────────┬────────┘
            ▼ validated
   ┌─────────────────┐
   │ Submit leg 2    │
   └────────┬────────┘
            ▼
   ┌─────────────────┐      leg 2 fail
   │ Wait validated  │─────────────→ RECOVERY FLOW
   └────────┬────────┘
            ▼ validated
   ┌─────────────────┐
   │ Record P&L      │
   │ Alert success   │
   └─────────────────┘
```

## Failure modes

| Failure point | State | Recovery |
|---|---|---|
| Pre-sim leg 1 fails | No state | Log, skip opp |
| Pre-sim leg 2 fails | No state | Log, skip opp |
| Submit leg 1 network error | No state | Log, skip opp |
| Leg 1 tx_hash never validates | Unknown | Wait `LastLedgerSequence` past — if never seen, no state |
| Leg 1 validated, leg 2 submit fails | Hold IOU | **Recovery flow** |
| Leg 1 validated, leg 2 validates with `tec*` | Hold IOU | **Recovery flow** |
| Both validate with `tesSUCCESS` | Complete | Record P&L |

## Mid-trade recovery flow (fully autonomous — never requires human)

When leg 1 completes but leg 2 does not, the bot holds an IOU it did not intend to hold long-term. Recovery is deterministic and self-resolving — no path requires human intervention.

1. **Immediate retry leg 2** with fresh rates. If the market hasn't moved more than 0.3%, the opp is still profitable. Max 2 retry attempts with fresh `LastLedgerSequence` each time.
2. **Fallback: market-dump the IOU** at any rate. Submit an IOU→XRP Payment with the held IOU as SendMax, accepting up to `RECOVERY_MAX_LOSS_PCT` (2%) of trade size as loss. This caps downside and exits the position.
3. **If market-dump also fails twice**: auto-halt for 2 hours via circuit breaker, blacklist the route for 24 hours, send informational Telegram alert with state dump. Bot resumes trading automatically after cooldown. The held IOU remains in the wallet; on next startup, the **bot-startup recovery guard** (below) will attempt market-dump again.

During recovery, the bot is in `MID_TRADE` state:
- No new opportunities are evaluated
- Heartbeat shows `state=MID_TRADE(leg2_pending)`
- Scan loop is paused
- Recovery runs on dedicated coroutine with 30s timeout per step

**Telegram alerts are informational only** — the bot never waits for human input. Alerts are logged and sent, but recovery proceeds regardless.

## Bot-startup recovery guard

On every bot startup, scan trust lines for non-zero IOU balances. If found:
1. Enter `MID_TRADE` state immediately
2. Run the market-dump recovery (step 2 above)
3. Only start normal scanning after wallet is clean (all IOU balances back to zero)

This handles the case where the bot crashed or was restarted while holding an IOU from a partially-completed trade.

## Autonomous halt policy

All halts are **time-boxed** and **auto-resume** — no halt state requires human action:

| Trigger | Halt duration | Resume condition |
|---|---|---|
| Daily loss ≥ 1% | 24h | Time elapsed + day boundary |
| 3 consecutive mid-trade failures | 2h | Time elapsed |
| Market-dump failed (recovery step 3) | 2h | Time elapsed, startup-guard re-attempts |
| 3 consecutive sim failures on same route | Route blacklist 24h only (bot keeps trading other routes) | Time elapsed |

Telegram alerts fire on all of these, but only as notifications. **The bot is designed to run unattended indefinitely.**

## Pre-simulation strategy

Both legs are simulated before either is submitted. This is the single most important safety measure.

**Challenge:** Leg 2's input depends on leg 1's output. Pre-simulating leg 2 before leg 1 executes requires predicting leg 1's output.

**Solution:**
1. Pre-sim leg 1 with its intended `SendMax` and target `Amount`. The simulate RPC returns the actual IOU amount delivered and XRP consumed.
2. Use that delivered IOU amount to construct leg 2's `SendMax` and `Amount`.
3. Pre-sim leg 2 against the simulated-post-leg-1 state. Rippled's `simulate` accepts a `ledger_index` override; we use the same ledger both simulations are keyed to.
4. If both return `tesSUCCESS` with profit ≥ threshold, proceed.

This means every live trade has effectively been validated against the live ledger twice before any real transaction is submitted.

## Sizing changes (optimized for autonomous operation)

Non-atomic risk is priced in by raising thresholds and lowering sizes. Values chosen to maximize consistent opportunity flow while eliminating paths that require human intervention.

| Parameter | Old | New | Rationale |
|---|---|---|---|
| `PROFIT_THRESHOLD` | 0.006 (0.6%) | 0.010 (1.0%) | Absorb 2× slippage + recovery-fail cost amortization. At 1%, even if 5% of trades fail recovery (capped at -2%), expected return stays strongly positive. |
| `PROFIT_THRESHOLD_HIGH_LIQ` | 0.003 (0.3%) | 0.006 (0.6%) | Same reasoning scaled to high-liq. Still below medium-liq to capture USD/USDC/EUR opportunities. |
| `MAX_POSITION_PCT` (first 7 days) | 0.05 (5%) | 0.02 (2%) | Caps blast radius of any single bad trade to ~2% of balance. After 7 days clean, auto-scales to 0.05. |
| `MIN_POSITION_PCT` | 0.01 (1%) | 0.01 (1%) | Unchanged |
| `MAX_TRADE_XRP_ABS` | — | `5.0` | **New**: absolute XRP cap per trade regardless of balance %. Defense against balance-calculation bugs. |
| `DAILY_LOSS_LIMIT_PCT` | 0.02 (2%) | 0.01 (1%) | Halts bot 24h on 1% daily loss. Stricter under non-atomic. |
| `MIN_BALANCE_GUARD_PCT` | — | 0.95 | **New**: skip all trades if current balance < 95% of reference. Defense against slow drain. |

## Route blacklist (autonomous)

`Blacklist` is extended with time-expiring entries:
- Any route (currency + cheap_issuer + rich_issuer triple) that fails simulate 3× in 1 hour → blacklist 24h
- Any route where mid-trade recovery fired → blacklist 24h
- All blacklist entries **auto-expire** — they re-enter the candidate pool after their TTL

This prevents the bot from repeatedly hitting the same broken path while keeping the strategy space open for profitable routes.

## Configuration additions

```env
# Two-leg execution tuning
LEG2_RETRY_MAX=2
LEG2_RETRY_SPREAD_TOLERANCE=0.003   # 0.3% spread drift allowed on retry
LEG2_TIMEOUT_LEDGERS=4              # ~20s wait for validation
RECOVERY_MAX_LOSS_PCT=0.02          # 2% max loss on emergency market-dump

# Autonomous safety rails
MAX_TRADE_XRP_ABS=5.0               # absolute XRP cap per trade
MIN_BALANCE_GUARD_PCT=0.95          # skip trades if balance < 95% of reference
MID_TRADE_HALT_HOURS=2              # cooldown after 3 mid-trade failures
ROUTE_BLACKLIST_HOURS=24            # route auto-unblocks after this
SIM_FAIL_BLACKLIST_COUNT=3          # consecutive sim fails before blacklist
SIM_FAIL_WINDOW_SECONDS=3600        # sliding window for counting sim fails

# Post-probation scaling (applied after 7 consecutive clean days)
PROBATION_DAYS=7
POST_PROBATION_MAX_POSITION_PCT=0.05  # auto-raise from 0.02 after probation
```

## What stays the same

- Volatility tracker (PR #17) — unchanged, feeds slippage calc
- Book-changes event-driven scan — unchanged
- Dust filter (PR #7) — unchanged
- Circuit breaker, blacklist — unchanged, tightened via config
- Simulator engine_result extraction (PR #15) — unchanged
- Preflight checks — extended to validate two-leg path builder

## Out of scope for this rewrite

- **CLOB-only vs AMM routing preference**: keep pathfinder's existing hybrid logic
- **AMMDeposit/AMMWithdraw direct arb**: separate future workstream
- **Cross-exchange arb (CEX+DEX)**: out of scope, different architecture entirely
- **Atomic Hooks amendment**: not yet mainnet, can revisit later

## Open questions for Phase B

1. Does pre-simulating leg 2 with a predicted post-leg-1 ledger state actually work on mainnet, or does rippled's `simulate` only work on the current validated ledger? Need empirical test during Phase D.
2. Should we cache the pre-sim result and skip the real simulation between Opportunity detection and execution? Probably yes for latency, but introduces staleness risk if any delay.
3. How do we handle the IOU balance post-leg-1 when it's a fractional amount that the bot doesn't track in its config? Need dynamic trust-line reading.
