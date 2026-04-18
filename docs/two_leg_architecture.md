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

## Mid-trade recovery flow

When leg 1 completes but leg 2 does not, the bot holds an IOU it did not intend to hold long-term. Recovery priority:

1. **Immediate retry leg 2** with fresh rates. If the market hasn't moved more than 0.3%, the opp is still profitable. Max 2 retry attempts with fresh `LastLedgerSequence` each time.
2. **Fallback: market-sell at any rate.** Submit an IOU→XRP Payment with the held IOU as SendMax, targeting the best current book rate. Accept a loss of up to 2% of the trade size. This caps the downside.
3. **If market-sell also fails twice**: halt the bot (`circuit_breaker.force_halt()`), send `CRITICAL` Telegram alert including held IOU balance, issuer, and last-known rates. Require manual intervention.

During recovery, the bot is in `MID_TRADE` state:
- No new opportunities are evaluated
- Heartbeat shows `state=MID_TRADE(leg2_pending)`
- Scan loop is paused

## Pre-simulation strategy

Both legs are simulated before either is submitted. This is the single most important safety measure.

**Challenge:** Leg 2's input depends on leg 1's output. Pre-simulating leg 2 before leg 1 executes requires predicting leg 1's output.

**Solution:**
1. Pre-sim leg 1 with its intended `SendMax` and target `Amount`. The simulate RPC returns the actual IOU amount delivered and XRP consumed.
2. Use that delivered IOU amount to construct leg 2's `SendMax` and `Amount`.
3. Pre-sim leg 2 against the simulated-post-leg-1 state. Rippled's `simulate` accepts a `ledger_index` override; we use the same ledger both simulations are keyed to.
4. If both return `tesSUCCESS` with profit ≥ threshold, proceed.

This means every live trade has effectively been validated against the live ledger twice before any real transaction is submitted.

## Sizing changes

Non-atomic risk is priced in by raising thresholds and lowering sizes:

| Parameter | Old | New | Rationale |
|---|---|---|---|
| `PROFIT_THRESHOLD` | 0.006 (0.6%) | 0.010 (1.0%) | Absorb 2× slippage + recovery-fail cost amortization |
| `PROFIT_THRESHOLD_HIGH_LIQ` | 0.003 (0.3%) | 0.006 (0.6%) | Same reasoning scaled to high-liq |
| `MAX_POSITION_PCT` (go-live) | 0.05 (5%) | 0.02 (2%) | First 7 days live only |
| `MIN_POSITION_PCT` | 0.01 (1%) | 0.01 (1%) | Unchanged |
| `DAILY_LOSS_LIMIT` | 0.02 (2%) | 0.01 (1%) | Stricter under non-atomic |

After 7 days of clean live trading, these can be loosened to prior levels (tracked as a follow-up).

## Configuration additions

```env
# Two-leg execution tuning
LEG2_RETRY_MAX=2
LEG2_RETRY_SPREAD_TOLERANCE=0.003   # 0.3% spread drift allowed on retry
LEG2_TIMEOUT_LEDGERS=4              # ~20s wait for validation
RECOVERY_MAX_LOSS_PCT=0.02          # 2% max loss on emergency market-sell
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
