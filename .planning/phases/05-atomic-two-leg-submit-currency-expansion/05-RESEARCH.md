# Phase 5: Atomic Two-Leg Submit + Currency Expansion — Research

**Researched:** 2026-04-20
**Domain:** XRPL transaction submission mechanics, Sequence management, atomic-submit architecture, liquid-IOU issuer selection
**Confidence:** HIGH (xrpl-py 4.5 source inspected directly; XRPL docs verified live)

## Summary

This phase rewrites the live execution path so both legs of an arbitrage trade are built, simulated, and signed **before** leg 1 hits the network, then submitted back-to-back using `submit`-only semantics so leg 2 is accepted by the local rippled node within tens of milliseconds of leg 1. The 5-7s drift that caused 4 `tecPATH_PARTIAL` losses on 2026-04-19 reduces to a single-round-trip WebSocket `submit` latency.

The XRPL primitives needed for this are all stable and well-documented. The `submit` RPC is already submit-only (it returns after local node acceptance, not ledger validation); the `Sequence` field is honored by `autofill` only when absent, so manual `N` / `N+1` assignment is trivial; and `simulate` accepts an unsigned tx with a future `Sequence` and evaluates it against current ledger state. The primary design choice is orphan handling for leg-1-fail — research recommends the **no-op AccountSet burn** as the determinism-safest option with negligible cost (12 drops).

**Primary recommendation:** Build two `TxDict` objects in a single pre-flight block, assign Sequences `N` (leg 1) and `N+1` (leg 2) from a single `account_info` call, align both `LastLedgerSequence = current_ledger + LEG2_TIMEOUT_LEDGERS` (default 4), run `simulate` on both (leg 1 must be `tesSUCCESS`; leg 2 must be `tesSUCCESS` OR `terPRE_SEQ` — the latter meaning "valid pending leg 1"), client-side sign both, then submit leg 1 → submit leg 2 with zero awaits between them. On leg-1 terminal failure, submit a no-op AccountSet at `Sequence N+1` to guarantee the pre-signed leg 2 blob can never replay.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Architecture — Atomic Submit:**
- Pre-sign both legs before leg 1 network submit. Leg 1 uses current Sequence N; leg 2 uses N+1. Both `LastLedgerSequence` windows aligned (same or adjacent ledger).
- Submission order: leg 1 submitted first, leg 2 submitted immediately after leg 1's submit call returns (do NOT wait for leg 1 validation). Submit-only (no validation wait) for leg 2 keeps the gap to network-round-trip latency.
- Simulate both legs before ANY submission. Leg 1 must return `tesSUCCESS`. Leg 2 is simulated at the CURRENT ledger state (pre-leg-1) and `terPRE_SEQ` is treated as pass (state-dependent).
- Atomic submit is the only live path. Previous sequential-submit path is REMOVED from `src/executor.py`. No feature flag, no fallback. DRY_RUN mode continues to log without submitting.

**Leg-1-Fail / Orphan Handling:**
- If leg 1 fails terminally (tec\*/tef\*/tem\*): the pre-signed leg 2 must NOT be replayable later. Researcher picks ONE of:
  1. Burn Sequence N+1 via a no-op AccountSet tx
  2. Let `LastLedgerSequence` expire naturally
  3. OfferCancel if leg 2 is an Offer-type tx

**Leg-2-Fail-After-Leg-1-Commits:**
- Existing 2% market-dump recovery flow is preserved as-is.

**Fee Strategy:**
- Both legs signed with the same fee (autofill default). No leg-1 priority escalation in v1.

**Single-Writer Guard:**
- Pre-submit check that no other tx is pending from this account during the arb window. Make implicit single-writer invariant explicit.

**Currency Expansion:**
- Minimum 2 additional currencies beyond USD/USDC/RLUSD/EUR.
- Each new currency needs a trusted issuer address documented in `.env.example`.
- `HIGH_LIQ_CURRENCIES` stays a comma-separated env string. Restart required.

**Dead-Knob Resolution:**
- `LEG2_TIMEOUT_LEDGERS` — wire into atomic-submit path as `LastLedgerSequence` offset for BOTH legs (replaces inline `+ 4`).
- `PROFIT_THRESHOLD_LOW_LIQ` — wire into `get_profit_threshold()` so non-HIGH_LIQ currencies return the LOW_LIQ threshold. Creates three-tier model (HIGH_LIQ → LOW_LIQ → base fallback).

**Testing:**
- Scenarios: both-legs-succeed, leg-1-fails-leg-2-sequence-burned, leg-2-fails-after-leg-1-commits (verifies 2% recovery still fires), simulate-leg-2-returns-terPRE_SEQ-treated-as-pass, fee-mismatch-between-legs-if-autofill-diverges.
- Replay test: against 4 failed trade hashes from 2026-04-19 (2EBD65E8, E8A24309, 1C63E5763115D09F, D6B62B3121F56901).
- Existing 194 tests must still pass.

**Logging:**
- Per-leg log entries with: leg number, Sequence, hash, engine_result, ledger_index, latency-from-leg1-submit.
- Trade-level summary entry on completion.
- Log schema change is ADDITIVE.

### Claude's Discretion

- Exact function signatures and method names in `src/executor.py`
- Internal structure (new `AtomicExecutor` class vs. refactor in-place)
- Specific issuer addresses for new currencies
- Test file naming convention
- Whether to emit new Telegram alerts for leg-1-fail vs. leg-2-fail cases (default: yes)

### Deferred Ideas (OUT OF SCOPE)

- Triangular / 3-leg arbitrage (architecture supports 2-leg only)
- Dynamic priority-fee escalation
- Multi-wallet / multi-account support
- Automatic volatility-based threshold adjustment
- Dashboard UI changes for per-leg visibility
- Backtester updates for atomic-submit semantics
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ATOM-01 | Both legs fully built and signed BEFORE leg 1 submission | Confirmed: `xrpl.core.keypairs.sign` on a fully-populated tx dict produces a standalone signed blob; identical to existing executor pattern at `src/executor.py:184-189`. Build+sign is ~1ms/leg. |
| ATOM-02 | Legs use sequential Sequence N, N+1 | `autofill()` in xrpl-py 4.5 **only** fills `sequence` when absent — [VERIFIED: xrpl-py 4.5 source, `xrpl/asyncio/transaction/main.py` autofill()]. Manual pre-set of `Sequence` is preserved. |
| ATOM-03 | Leg 2 submitted immediately after leg 1 returns (no wait) | `submit()` internally calls `SubmitOnly(tx_blob=...)` which returns after local node acceptance, NOT after ledger validation — [CITED: https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/submit]. Latency ≈ single WebSocket round-trip. |
| ATOM-04 | Leg-1 terminal fail → leg-2 cancelled or Sequence burned | Three options evaluated below; recommended: no-op AccountSet burn at Sequence N+1. [CITED: https://xrpl.org/docs/concepts/transactions/finality-of-results/canceling-a-transaction] |
| ATOM-05 | Leg-2-fail-after-leg-1-commits → existing 2% recovery activates | Current `CircuitBreaker.record_trade(profit_xrp)` in `src/safety.py:80` already handles negative profit tracking. No change required. [VERIFIED: src/safety.py inspection] |
| ATOM-06 | Single-writer guard | Recommended: pre-flight `account_info` returning Sequence N, then compare vs. any prior submit-tracking state. Leverages existing main.py asyncio.Lock. |
| ATOM-07 | Simulate both legs; leg-2 `terPRE_SEQ` treated as pass | `simulate` is an unsigned-tx dry run [CITED: https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/simulate]. `terPRE_SEQ` = "Sequence ahead of current account Sequence" [CITED: https://xrpl.org/ter-codes.html]. Combined: a leg-2 dry run at pre-leg-1 state returns `terPRE_SEQ` when leg 2's Sequence is N+1 but account Sequence is N — this is the expected state-dependent pass. |
| ATOM-08 | Decimal arithmetic preserved | Existing `_build_tx_dict` at `src/executor.py:40` already uses Decimal. Atomic-submit rewrite inherits this pattern. [VERIFIED: src/executor.py:51-54] |
| ATOM-09 | Per-leg log entries with Sequence, hash, engine_result, ledger_index | Existing `log_trade()` in `src/trade_logger.py` is schema-flexible (kwargs → JSONL). Additive change only. [VERIFIED: src/trade_logger.py:41-64] |
| ATOM-10 | Atomic submit replaces sequential path (no dead code) | Only two callers of `executor.execute(opp)`: `main.py:69` and `tests/test_executor.py`. Signature stable; internals fully rewritten. [VERIFIED: grep] |
| CURR-01 | HIGH_LIQ_CURRENCIES expanded beyond USD/USDC/RLUSD/EUR | Recommended additions: **SOLO** (Sologenic, `rsoLo2S1...`) and **USDT** (GateHub, `rcvxE9PS9...`). Both already have trust lines provisioned in `scripts/setup_trust_lines.py`. [VERIFIED: setup_trust_lines.py:63, 76] |
| CURR-02 | Currency add/remove via `.env` only | `HIGH_LIQ_CURRENCIES` is an env-parsed list in `src/config.py:48-50`. Used only in `profit_math.get_profit_threshold()` — no code changes required to add entries. [VERIFIED: grep] |
| CURR-03 | Every currency has documented issuer address | `.env.example` currently documents none. Plan must add an issuer map section covering all six HIGH_LIQ currencies. |
| CLEAN-01 | `LEG2_TIMEOUT_LEDGERS` wired or removed | Note: CONTEXT.md states this var exists at `src/config.py:49` — **IT DOES NOT**. That line is `HIGH_LIQ_CURRENCIES`. CONTEXT is stale vs. current branch state. Plan must ADD `LEG2_TIMEOUT_LEDGERS` to `config.py` + `.env.example`, then consume it in the atomic-submit `LastLedgerSequence` calc. [VERIFIED: config.py line-by-line + grep for `LEG2`] |
| CLEAN-02 | `PROFIT_THRESHOLD_LOW_LIQ` wired into `get_profit_threshold()` | Currently imported but unused. `get_profit_threshold()` at `src/profit_math.py:74` only returns HIGH_LIQ or base. Three-tier branch: HIGH_LIQ → return HIGH_LIQ; LOW_LIQ explicit list → return LOW_LIQ; else → base PROFIT_THRESHOLD. Requires new `LOW_LIQ_CURRENCIES` env var OR treat "not in HIGH_LIQ" as LOW_LIQ (simpler). [VERIFIED: profit_math.py:74-87] |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Decimal-only math** — all monetary calculations use `decimal.Decimal`. Never use float in leg construction, fee math, or LastLedgerSequence arithmetic. (LastLedgerSequence is an int, not a Decimal — but any fee/amount math must stay Decimal.)
- **VPS resources** — 1 CPU, 4GB RAM. Do not add concurrent pre-signing worker pools. Two legs in sequence on the main loop is fine (~2ms total signing work).
- **Safety-first** — DRY_RUN=True remains the default. Atomic-submit code only executes when `DRY_RUN=False` and `XRPL_SECRET` is set.
- **Minimal deps** — do NOT add new packages. Everything needed is already in `xrpl-py>=3.0.0` (current installed: 4.5.0).
- **No secrets in code** — SOPS key unchanged; issuer addresses (public, non-secret) go in `.env.example`.

## Project Skills

None found (no `.claude/skills/`, `.agents/skills/`, etc.).

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| xrpl-py | 4.5.0 (installed) | XRPL client — signing, encoding, submit | Official library, maintained by XRPLF. Already in project. [VERIFIED: `pip show xrpl-py` returns 4.5.0] |
| `xrpl.core.binarycodec` | — | `encode()`, `encode_for_signing()` | Canonical serialization. Used in current executor.py:22. |
| `xrpl.core.keypairs` | — | `sign()` — pure-function tx signing | Used in current executor.py:23 — proven path. |
| `xrpl.wallet.Wallet` | — | Seed → address/public/private key | Used in current executor.py:24. |

### Supporting (from stdlib / already installed)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `asyncio` | stdlib | Concurrent submit calls | For parallel leg 1 + leg 2 `submit()` if we choose the parallel-submit optimization (see Open Question below) |
| `decimal.Decimal` | stdlib | All monetary math | Preserved throughout atomic-submit rewrite |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `xrpl-py` high-level models (`Payment`, `AccountSet`) | Raw tx dicts (current pattern) | xrpl-py's `Payment` model rejects same-account XRP→XRP with paths (documented at `src/executor.py:13-15`). Must continue using raw dicts for leg 1. Leg 2 (if also same-account cross-currency) same constraint. No-op AccountSet CAN use the `AccountSet` model since it's a plain-account tx with no path-routing. |
| Client-side autofill via `xrpl.asyncio.transaction.autofill` | Manual Sequence/Fee/LastLedgerSequence | Current code does manual — bypasses model validation. Keep manual for legs; can use `autofill()` for the no-op AccountSet burn because the `AccountSet` model IS compatible. |
| `submit_and_wait` | `submit` (current) | `submit_and_wait` waits for validation — wrong semantic for atomic submit. `submit` returns on node acceptance, which is what we want. [CITED: xrpl-py 4.5 source — `submit_and_wait` calls `submit()` then polls until validated.] |

**No new installs required.**

**Version verification:**
```bash
pip show xrpl-py
# Name: xrpl-py
# Version: 4.5.0  # [VERIFIED 2026-04-20]
```

## Architecture Patterns

### Recommended Flow Structure
```
executor.execute(opportunity)
├── [existing] circuit breaker + blacklist gate
├── [new] build_atomic_pair(opp) → (leg1_tx_dict, leg2_tx_dict)
│   ├── one account_info call → Sequence N
│   ├── leg1: tx_dict with Sequence=N, LastLedgerSequence=current+LEG2_TIMEOUT_LEDGERS
│   └── leg2: tx_dict with Sequence=N+1, same LastLedgerSequence
├── [new] simulate_pair(leg1, leg2) → (sim1, sim2)
│   ├── leg1 must be tesSUCCESS
│   └── leg2 must be tesSUCCESS OR terPRE_SEQ
├── [reused] sign both legs locally (bytes.fromhex + keypairs_sign)
├── [new] atomic_submit(leg1_blob, leg2_blob)
│   ├── await submit(leg1_blob)       # returns on node accept
│   ├── record leg1 engine_result
│   ├── if leg1 terminal-fail: burn_sequence(N+1); return failure
│   ├── await submit(leg2_blob)       # also returns on node accept
│   └── record leg2 engine_result
├── [new] log_pair(leg1_result, leg2_result, trade_summary)
└── [reused] circuit_breaker.record_trade(net_profit_xrp)
```

### Pattern 1: Manual Sequence override through autofill
**What:** Set `Sequence`/`Fee`/`LastLedgerSequence` in tx dict BEFORE calling any sign/submit. xrpl-py's autofill leaves pre-set fields alone.

**When to use:** Any time you need deterministic, pre-computed Sequence values — e.g., atomic multi-leg submits, Ticket-based batches.

**Source (xrpl-py 4.5):**
```python
# File: xrpl/asyncio/transaction/main.py (simplified from actual source)
async def autofill(transaction, client, signers_count=None):
    transaction_json = transaction.to_dict()
    ...
    if "sequence" not in transaction_json:                    # <-- only fills if absent
        sequence = await get_next_valid_seq_number(...)
        transaction_json["sequence"] = sequence
    if "fee" not in transaction_json:                         # <-- only fills if absent
        transaction_json["fee"] = await _calculate_fee_per_transaction_type(...)
    if "last_ledger_sequence" not in transaction_json:        # <-- only fills if absent
        ledger_sequence = await get_latest_validated_ledger_sequence(client)
        transaction_json["last_ledger_sequence"] = ledger_sequence + _LEDGER_OFFSET
    ...
```
[VERIFIED: pulled directly via `inspect.getsource` on xrpl-py 4.5.0 installed in this environment]

Note: the current bot uses **raw dicts** (not `Transaction` models) because of the cross-currency XRP→XRP paths validation restriction. For raw-dict flow we don't call `autofill()` — we set Sequence/Fee/LastLedgerSequence directly. The confirmed autofill behavior above still matters because any helper (e.g., for the no-op AccountSet burn) can safely use `autofill()` without clobbering a pre-set Sequence.

### Pattern 2: Sequence burn via no-op AccountSet
**What:** An AccountSet transaction with zero options is the canonical XRPL no-op. Submit one with `Sequence = N+1` and the pre-signed leg 2 (also at N+1) can never apply — the ledger will include either the burn or leg 2, not both.

**When to use:** Leg 1 returns a terminal tec/tef/tem — the pre-signed leg 2 blob is still floating. Burn closes the door.

**Example:**
```python
# Pseudocode — do not copy verbatim
burn_tx = {
    "TransactionType": "AccountSet",
    "Account": wallet.address,
    "Sequence": N + 1,
    "Fee": "12",
    "LastLedgerSequence": current_ledger + LEG2_TIMEOUT_LEDGERS,
    # No SetFlag / ClearFlag / Domain / EmailHash / etc. — no-op
    "SigningPubKey": wallet.public_key,
}
# sign + submit the burn — leg 2 is now permanently blocked
```
[CITED: https://xrpl.org/docs/concepts/transactions/finality-of-results/canceling-a-transaction]
> "send another transaction from the same sending address with the same Sequence value ... an AccountSet transaction with no options is the canonical no-op"

### Pattern 3: `submit` = node-accept, not ledger-validated
**What:** `xrpl.asyncio.transaction.submit(transaction, client)` internally sends a `SubmitOnly` RPC. Returns as soon as the local rippled node accepts the tx into the open ledger / queue / broadcast — does **not** wait for validation.

**Source (xrpl-py 4.5):**
```python
async def submit(transaction, client, *, fail_hard=False):
    transaction_blob = encode(transaction.to_xrpl())
    response = await client._request_impl(
        SubmitOnly(tx_blob=transaction_blob, fail_hard=fail_hard)
    )
    ...
```
[VERIFIED: xrpl-py 4.5.0 source — `xrpl/asyncio/transaction/main.py`]

**Response fields relevant to atomic submit:**
- `engine_result` — provisional result (tesSUCCESS, tecPATH_PARTIAL, terPRE_SEQ, etc.)
- `engine_result_code` — numeric variant
- `accepted`, `applied`, `broadcast`, `kept`, `queued` — booleans about local node decision
- `validated_ledger_index` — current (not the tx's) validated ledger
[CITED: https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/submit]

**Critical implication:** leg 1's `submit` returning `tesSUCCESS` is **provisional**. The final outcome is only known when a later `tx` lookup returns `"validated": true`. For atomic submit this is fine — we submit leg 2 on provisional leg-1 acceptance and trust the probability that both will validate together.

### Anti-Patterns to Avoid

- **Calling `submit_and_wait` on leg 1:** would block until leg 1 validated, reintroducing the 5-7s drift we're eliminating.
- **Using `autofill()` on the leg tx dicts:** the raw-dict approach in the current code bypasses `Payment`-model validation that fails for same-account XRP→XRP with paths. Keep using raw dicts for legs.
- **Calling `account_info` twice** (once per leg) for Sequence: a second call risks returning the post-leg-1 Sequence if consensus happens to complete mid-flight. ONE call up front.
- **Asserting the post-submit `tx_json.hash` from leg 1 into leg 2's fields:** hashes are computed from the signed blob and don't chain. Each leg's hash is independent.
- **Assuming leg 2 simulate `tesSUCCESS` means leg 2 will commit:** simulate only tests the transaction in isolation against current state. When leg 2's Sequence is N+1 and current Sequence is N, `terPRE_SEQ` is the authoritative "state-dependent pass." If simulate returns `tesSUCCESS` despite Sequence=N+1, it means the node autofilled Sequence (ignoring our override) — investigate before trusting.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Tx signing | Custom ed25519/secp256k1 signer | `xrpl.core.keypairs.sign` (already used) | Handles SigningPubKey prefix, hash-prefix bytes, signature encoding |
| Tx serialization | Hand-rolled field ordering | `xrpl.core.binarycodec.encode` + `encode_for_signing` (already used) | Canonical field ordering & type codes — wrong order = invalid blob |
| Sequence reservation | Custom mempool-inspection layer | Pre-flight `account_info` + single-writer main-loop invariant | XRPL nodes don't expose mempool via public RPC. The `account_info` "current" Sequence is the authoritative next-free number; the existing `asyncio.Lock` in main.py already enforces single-writer. |
| Fee calculation | Custom `fee` RPC + load_factor math | Hardcoded `"12"` drops (current pattern) OR xrpl-py's `_calculate_fee_per_transaction_type` | Current code uses hardcoded 12-drop fee (NETWORK_FEE constant). Under mainnet congestion the real cost can rise, but 12 drops is the universal minimum and the bot historically has not been queued. Keep the hardcode for v1; document as a known limit. |
| LastLedgerSequence offset | Hand-tuned offset | `_LEDGER_OFFSET = 20` (xrpl-py default) OR `LEG2_TIMEOUT_LEDGERS` env var | xrpl-py's default is 20 ledgers (~80s). Current code uses 4 (~20s) — aggressive but matches the arb-short-life requirement. Expose as `LEG2_TIMEOUT_LEDGERS` for tunability. |
| Historical book state | Custom re-run of ripple_path_find against arbitrary ledger | `book_offers` RPC with `ledger_index` parameter — queries past ledger state on full-history nodes like s2.ripple.com | [CITED: https://xrpl.org/docs/concepts/networks-and-servers/ledger-history — full history via s2.ripple.com.] Used for the 2026-04-19 replay test. |
| Orphan Sequence cleanup | Custom "wait 4 ledgers for LastLedgerSequence expiry" logic | No-op AccountSet burn (see Pattern 2) | The expire-naturally option is VALID but leaves a signed blob that could theoretically be replayed by a compromised admin until expiry. The burn deterministically closes the window in < 1 ledger. Cost: 12 drops. |

**Key insight:** Every primitive in this phase already has a production-grade implementation in xrpl-py 4.5 or the current codebase. The phase is *composition*, not *invention*. Resist the urge to write a new `AtomicSubmitter` class with its own state machine; refactor `TradeExecutor.execute()` in place.

## Runtime State Inventory

> Phase 5 is a code refactor + config expansion. No data migrations, but several non-code surfaces must be audited.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | JSONL log `xrpl_arb_log.jsonl` already exists — atomic-submit adds new fields (`leg`, `sequence`, `hash`, `engine_result`, `ledger_index`, `latency_from_leg1_ms`). Readers (Streamlit dashboard, backtester) tolerate unknown keys (they read specific fields by name). | Additive schema change. Verify dashboard/backtester by running them against a post-rewrite log and confirming no KeyError exceptions. |
| Live service config | `.env` on VPS at `/opt/xrplbot/.env` — `HIGH_LIQ_CURRENCIES` defaults to 4-currency list. After deploy, either (a) operator edits `.env` to include SOLO,USDT OR (b) `src/config.py` default is updated to 6-currency list (recommended — gives new currencies without operator action). | Update both `src/config.py` default AND `.env.example` inline. Operator's existing `.env` is untouched (no breaking change); they opt in by adding the new vars. |
| OS-registered state | `systemd` unit `xrplbot.service` — no dependency on atomic-submit internals. | None. |
| Secrets/env vars | `XRPL_SECRET` — unchanged. New non-secret env var `LEG2_TIMEOUT_LEDGERS` (default 4). | Add to `.env.example` with explanatory comment. |
| Build artifacts | No compiled artifacts (pure Python). `__pycache__` auto-refreshes on next import. | None. |

## Common Pitfalls

### Pitfall 1: Leg-2 simulate result conflated with leg-1 semantics
**What goes wrong:** Developer sees `terPRE_SEQ` on leg 2 simulate and treats it as failure, aborting the trade.

**Why it happens:** `terPRE_SEQ` reads like a negative result. But by definition leg 2's Sequence is N+1 while account Sequence is N — the simulate is evaluating leg 2 before leg 1 has applied. A retryable "future" condition.

**How to avoid:** Explicit handler in `simulate_pair`:
```python
ACCEPTABLE_LEG2_SIM_RESULTS = {"tesSUCCESS", "terPRE_SEQ"}
```
**Warning signs:** Zero trades executing post-rewrite despite leg-1 simulates passing; log shows `"Simulation failed (WS): terPRE_SEQ"` from the current `simulate_transaction_ws` code path (which returns `success=False` for anything other than exact `tesSUCCESS` — see `src/simulator.py:98,172`).

**Implication for plan:** the existing `SimResult` helper treats non-`tesSUCCESS` as failure. Plan must add a leg-2-specific gate that accepts `terPRE_SEQ` OR use a new helper that takes an "acceptable codes" whitelist.

### Pitfall 2: `LastLedgerSequence` mismatch between legs
**What goes wrong:** Leg 1's window set from `account_info`'s `ledger_current_index`; leg 2's window set from a subsequent `server_info` call. The two reference ledgers differ by 1, so leg 2's window is offset by 1. Leg 1 commits at ledger X+3, leg 2's window expired at X+2 → leg 2 rejected with `tefMAX_LEDGER`.

**Why it happens:** Two separate RPC calls returning different "current" ledger indexes.

**How to avoid:** ONE `account_info` call returns BOTH `Sequence` and `ledger_current_index`. Use the same `ledger_current_index` for BOTH legs' `LastLedgerSequence = ledger_current_index + LEG2_TIMEOUT_LEDGERS`.

**Warning signs:** Post-deploy, intermittent `tefMAX_LEDGER` on leg 2 with leg 1 `tesSUCCESS`. Tests must assert identical `LastLedgerSequence` between legs.

### Pitfall 3: Autofill silently overrides pre-set Sequence
**What goes wrong:** Developer builds tx dict with `Sequence=N+1`, then calls `autofill_and_sign(tx, client, wallet)` thinking it's a convenience wrapper. xrpl-py's autofill checks `"sequence" not in transaction_json` — but internally uses snake_case. Passing `Sequence` (capital S) in CamelCase dict would *not* match `"sequence"` (snake_case) check — autofill would think sequence is missing and overwrite it.

**Why it happens:** xrpl-py's `Transaction.to_dict()` normalizes to snake_case. A raw dict passed through a model conversion path can silently get re-keyed.

**How to avoid:** For raw-dict flow (the current leg-1 / leg-2 path), DO NOT call `autofill` or `autofill_and_sign`. Use `encode_for_signing` + `keypairs_sign` + `encode` directly (current pattern at `src/executor.py:184-189`). This bypasses model validation and the autofill/case conversion entirely.

**Warning signs:** Both legs signed with the same Sequence (conflict) or mysterious `tefPAST_SEQ` / `terPRE_SEQ` on legs you thought were correct.

### Pitfall 4: Fee drift between legs under network load
**What goes wrong:** Two separate fee calculations between pre-sign of leg 1 and leg 2 return different fees (e.g., `load_factor` spiked). Different fees in the two txs doesn't break atomicity, but creates audit confusion.

**Why it happens:** XRPL's `load_factor` can change between calls [CITED: https://xrpl.org/docs/concepts/transactions/transaction-cost].

**How to avoid:** Force uniform fee across both legs. Bot already hardcodes `"Fee": "12"` (NETWORK_FEE const = 12 drops) — keep this for v1. Both legs use the same value, end of story.

**Warning signs:** If future work introduces dynamic fees, capture fee once up front and reuse for both legs.

### Pitfall 5: `simulate` on leg 2 returning `tesSUCCESS` (when `terPRE_SEQ` is expected)
**What goes wrong:** Developer sees leg 2 sim passes with `tesSUCCESS` and assumes everything is fine. Actually, this can mean the node's simulate silently autofilled Sequence, ignoring the override — leg 2's blob as submitted has Sequence=N+1 but the simulation evaluated Sequence=N (which conflicts with leg 1).

**Why it happens:** [CITED: https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/simulate] — "If the Fee, Sequence, SigningPubKey, or NetworkID fields are provided, they will be used in the transaction. Otherwise, the server will autofill them." If provided, they SHOULD be used — but bugs in rippled versions have historically mishandled edge cases. Better to verify behavior empirically than assume.

**How to avoid:** Add a test that submits leg 2 simulate with `Sequence` explicitly set to `account.Sequence + 1` and asserts the response's `tx_json.Sequence` equals N+1 (not N). Catches silent autofill overrides.

**Warning signs:** `sim2.raw["tx_json"]["Sequence"] != N+1` → investigate; the node may be ignoring the override.

### Pitfall 6: CONTEXT.md describes config state that doesn't match branch
**What goes wrong:** CONTEXT.md states `LEG2_TIMEOUT_LEDGERS` is at `src/config.py:49`. It is not. That line is `HIGH_LIQ_CURRENCIES`. Also states the var is "defined but not wired" — actually the var is **not defined at all** on the current branch.

**Why it happens:** Memory notes were captured before a config cleanup on the branch (or CONTEXT was copy-pasted from an older snapshot).

**How to avoid:** Plan must treat CLEAN-01 as "ADD `LEG2_TIMEOUT_LEDGERS` to `config.py` and `.env.example`, then wire it" — NOT "remove dead definition and wire." Verify with grep before writing tasks.

**Warning signs:** Planner attempts `Edit` on a non-existent line.

## Code Examples

### Single `account_info` for both legs' Sequence + ledger
```python
# Canonical atomic pre-flight — one RPC call, both pieces of state
# Source pattern: current src/executor.py:152-176 (adapted)

account_info_response = await connection.send_raw({
    "command": "account_info",
    "account": wallet.address,
    "ledger_index": "current",
})
acct_result = account_info_response.get("result", account_info_response) or {}
if "account_data" not in acct_result:
    # abort — can't build legs without Sequence
    return False

sequence_n = acct_result["account_data"]["Sequence"]    # leg 1 uses N
sequence_n_plus_1 = sequence_n + 1                      # leg 2 uses N+1
current_ledger = acct_result.get("ledger_current_index", 0)
last_ledger = current_ledger + LEG2_TIMEOUT_LEDGERS     # IDENTICAL for both legs
```
[VERIFIED: pattern mirrors current src/executor.py:152-176]

### Leg-2 simulate gate that accepts `terPRE_SEQ`
```python
# Adapted from src/simulator.py:98-103 — add terPRE_SEQ branch for leg-2 use
ACCEPTABLE_LEG2_CODES = {"tesSUCCESS", "terPRE_SEQ"}

def is_leg2_sim_pass(sim_result) -> bool:
    """Leg 2 sim passes if tesSUCCESS OR terPRE_SEQ (state-dependent pass)."""
    return sim_result.result_code in ACCEPTABLE_LEG2_CODES
```

### No-op AccountSet Sequence burn
```python
# Orphan handler — run if leg 1 returns terminal tec/tef/tem.
# Uses xrpl-py model (Payment constraint doesn't apply to AccountSet).

from xrpl.asyncio.transaction import autofill_and_sign, submit
from xrpl.models.transactions import AccountSet

async def burn_sequence(
    sequence_to_burn: int,
    wallet: Wallet,
    client,
    last_ledger_sequence: int,
) -> str | None:
    """Submit no-op AccountSet at given Sequence; returns tx hash or None."""
    burn = AccountSet(
        account=wallet.address,
        sequence=sequence_to_burn,
        fee="12",
        last_ledger_sequence=last_ledger_sequence,
    )
    # Use high-level helper — AccountSet has no model restrictions
    signed = await autofill_and_sign(burn, client, wallet, check_fee=False)
    try:
        response = await submit(signed, client)
        engine_result = response.result.get("engine_result", "unknown")
        logger.info(f"Burn Sequence {sequence_to_burn}: {engine_result}")
        return response.result.get("tx_json", {}).get("hash")
    except Exception as e:
        logger.error(f"Burn submit failed: {e}")
        return None
```

### Replay test structure — 2026-04-19 incident data
```python
# Pseudocode — test harness outline for replay

INCIDENT_HASHES = [
    "2EBD65E8...", "E8A24309...",
    "1C63E5763115D09F...", "D6B62B3121F56901...",
]

async def test_atomic_would_have_succeeded():
    for tx_hash in INCIDENT_HASHES:
        # 1. Look up the incident tx to find its ledger_index
        tx = await rpc.request({"method": "tx", "params": [{"transaction": tx_hash}]})
        incident_ledger = tx["result"]["ledger_index"]

        # 2. Fetch book_offers at incident_ledger - 1 (snapshot BEFORE leg 1 applied)
        book = await rpc.request({
            "method": "book_offers",
            "params": [{
                "taker_gets": {"currency": "XRP"},
                "taker_pays": {"currency": "USD", "issuer": "<GateHub>"},
                "ledger_index": incident_ledger - 1,
            }],
        })

        # 3. Simulate atomic leg-1 + leg-2 at that ledger
        #    (build both legs from incident_ledger - 1 book state)
        leg1, leg2 = build_legs_from_book(book, incident_xrp_amount)
        sim1 = await simulate_at_ledger(leg1, incident_ledger - 1)
        sim2 = await simulate_at_ledger(leg2, incident_ledger - 1)

        # 4. Assert both would have passed the pre-submit gate
        assert sim1.engine_result == "tesSUCCESS"
        assert sim2.engine_result in {"tesSUCCESS", "terPRE_SEQ"}
```

**Caveat:** The simulate RPC evaluates against **current** ledger state, not an arbitrary past ledger. To replay against a past ledger, we'd need either a `simulate`-with-ledger_index parameter (not present in current XRPL per docs) OR to reconstruct the arb math from historical `book_offers` and check whether leg 2's required rate was available at `incident_ledger`. The latter is the realistic approach.

**Realistic replay test:** fetch `book_offers` at `incident_ledger - 1`, compute leg 2's output using current pathfinder math, verify `output >= required_for_profit`. This is NOT a perfect simulation but it's a strong empirical signal.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Sequential submit with re-`ripple_path_find` between legs | Atomic pre-signed two-leg submit | This phase (2026-04) | Eliminates 5-7s drift that caused 100% leg-2 failure rate on 2026-04-19 |
| `sign_and_submit` (single helper) | `submit` (submit-only) | Required by atomic pattern | Gives us control over submit timing between legs |
| Hardcoded 4-ledger window (`+ 4`) | `LEG2_TIMEOUT_LEDGERS` env var | This phase | Tunable per operator; removes dead knob |
| Two-tier threshold (HIGH_LIQ + base) | Three-tier (HIGH_LIQ → LOW_LIQ → base) | This phase | Wires in the LOW_LIQ knob that has been dead since Phase 1 |

**Deprecated/outdated:**
- The sequential pre-flight pattern (build leg 2 after leg 1 commits) — removed by ATOM-10.
- `_LEDGER_WINDOW = 4` inline constant — replaced by env var.
- Empty branch in `get_profit_threshold` for non-HIGH_LIQ currencies returning base threshold — replaced by LOW_LIQ branch.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | XRPL's `terPRE_SEQ` is deterministically returned when submitting a tx with Sequence ahead of current account Sequence, rather than some other temporary-retry code like `terRETRY` | Pitfall 1 / ATOM-07 | If `simulate` returns a different code (e.g., `terRETRY` or `telCAN_NOT_QUEUE`), the whitelist `{tesSUCCESS, terPRE_SEQ}` misses the valid state and all leg-2 sims fail. Mitigation: add a manual test on testnet (or mainnet DRY_RUN) that confirms the exact code returned. |
| A2 | `submit` RPC on public mainnet node `s2.ripple.com` returns in < 200ms for a valid signed blob, so leg-2 submit follows leg-1 submit within ~1 ledger close | Primary recommendation | If round-trip is slow (>2s), drift returns in a reduced form. Mitigation: add latency logging between leg-1-submit-return and leg-2-submit-return; alert if > 1000ms. |
| A3 | XRPL public full-history nodes (s2.ripple.com) retain `book_offers` queryable at `ledger_index` = 2026-04-19 incident ledgers (~3 weeks ago) | Replay test | If not retained on s2, must use a different historical node (e.g., QuickNode archival RPC — paid). Mitigation: try the `book_offers` call before writing the full test harness; if fails, replay test is empirical-only via live future volatility burst. |
| A4 | Hardcoded 12-drop fee remains sufficient under mainnet load (never triggers `telINSUF_FEE` on our 1-CPU VPS) | Fee Strategy | If mainnet congestion spikes during a trade, one leg could be queued or rejected. Historical bot has not hit this. Mitigation: existing `queued` engine_result would be logged and visible in post-trade audit; if encountered, upgrade to dynamic fee (deferred). |
| A5 | `HIGH_LIQ_CURRENCIES=USD,USDC,RLUSD,EUR,SOLO,USDT` gives meaningful route expansion without introducing illiquid spreads | CURR-01 | Adding an illiquid currency could produce dust-level slippage. SOLO has ~$1M+ daily DEX volume per xrpl.to; USDT (GateHub) is lower — confirm via book_offers pre-deploy. Mitigation: existing dust-filter (PR #7) would reject thin opportunities; threshold gate (`is_profitable`) filters the rest. |

## Open Questions (RESOLVED)

All six open questions have been answered by decisions now locked into Plans 05-01 through 05-05. Resolutions inline below (per plan-checker Dimension 11 revision, 2026-04-20).

1. **`terPRE_SEQ` vs other ter codes on leg 2 simulate** — CONTEXT says current code already treats `terPRE_SEQ` as pass. After reading `src/simulator.py`, the current simulator does NOT have this branch — it accepts only exact `tesSUCCESS`. The CONTEXT claim appears to be wishful memory. ACTION: Planner must explicitly add a new simulate helper (or a flag to the existing one) that accepts a whitelist of codes. Confirm with user if unclear.

   **RESOLVED:** Plan 05-02 adds a new module-level helper `is_acceptable_sim_result(result_code: str, *, is_leg_2: bool)` and a constant `LEG2_ACCEPTABLE_CODES = frozenset({"tesSUCCESS", "terPRE_SEQ"})` in `src/simulator.py`. Leg 1 uses `is_leg_2=False` (strict `tesSUCCESS`); leg 2 uses `is_leg_2=True` (whitelist). Existing `SimResult.success` semantics are unchanged — the helper is additive. See 05-02 Task 1.

2. **Replay test harness — simulation vs. book math** — `simulate` RPC does not appear to accept a `ledger_index` parameter for historical evaluation. Plan should default to: fetch historical `book_offers` and re-run pathfinder math to determine whether the atomic submit's required rates would have been available. If user insists on a true simulate-at-past-ledger, this is LOW confidence (may not be possible with public nodes).

   **RESOLVED:** Plan 05-05 uses a fixture-based replay approach, NOT live historical RPC. `tests/fixtures/incident_2026_04_19/hashes.json` carries the 4 incident trade hashes + approximate opportunity shapes. The replay test constructs an `Opportunity` per fixture, feeds it to the atomic executor with mocked WS responses, and asserts the atomic flow submits both legs without a drift window. This proves the TIMING fix (the actual bug); the README documents why historical `book_offers` replay is out of scope. See 05-05 Tasks 1 and 2.

3. **LOW_LIQ classification boundary** — for CLEAN-02 wire-in, the simplest model is "HIGH_LIQ explicit list → HIGH_LIQ threshold; everything else → LOW_LIQ threshold; base PROFIT_THRESHOLD becomes unused." This effectively removes the "base" tier. ALTERNATIVE: add a new `LOW_LIQ_CURRENCIES` env var and keep the base tier as default for unlisted currencies. Simpler = replace base with LOW_LIQ by default. Recommend the former but confirm with user.

   **RESOLVED:** Plan 05-01 Task 3 adopts the simpler model — "non-HIGH_LIQ → LOW_LIQ", no separate `LOW_LIQ_CURRENCIES` env var. The base `PROFIT_THRESHOLD` is retained only as the env-default fallback for `PROFIT_THRESHOLD_LOW_LIQ` (0.010). Users who want a single-threshold setup can set both HIGH_LIQ and LOW_LIQ to the same value. The docstring in `get_profit_threshold()` explains this three-tier structure explicitly.

4. **New currencies beyond SOLO, USDT** — both already have trust lines. Research could justify adding CORE (Coreum, also already trust-lined), XAU, or GBP. But: CURR-01 requires only "SOLO plus one more." Recommend conservative: `SOLO,USDT`. Adding more can be done via `.env` later without code changes (CURR-02).

   **RESOLVED:** Plan 05-01 Task 2 ships the default as `USD,USDC,RLUSD,EUR,SOLO,USDT` (6 currencies — minimum satisfied by SOLO + USDT per CURR-01). Any further expansion is a pure `.env` config change (CURR-02 env-only-reload contract is test-locked in `test_high_liq_env_override_reloads`). Operator can add CORE/XAU/GBP later without touching code.

5. **Parallel submit (leg 1 + leg 2 concurrently via `asyncio.gather`)** — would reduce the already-small gap further. BUT: if leg 1 fails immediately, we'd have already submitted leg 2 to the network before we can decide to burn — orphan handling becomes more complex. Recommend: SEQUENTIAL submit (`await submit(leg1); await submit(leg2)`) with explicit leg-1-result branch. Latency cost is ~1 round-trip ≈ 100-200ms, still orders of magnitude better than 5-7s.

   **RESOLVED:** Plan 05-03 Task 2 uses SEQUENTIAL submit (`await self._submit_blob(leg1_blob); ...; await self._submit_blob(leg2_blob)`). Parallel submit via `asyncio.gather` was rejected because the leg-1-terminal-fail branch requires knowing leg-1's engine_result BEFORE deciding whether to submit leg 2 or to burn Sequence N+1. The 100-200ms sequential cost is acceptable against the 5-7s bug we're fixing.

6. **Backward compatibility for `TradeExecutor.execute()` signature** — only two callers: `main.py:69` and `tests/test_executor.py`. No signature change needed (still `async def execute(self, opportunity) -> bool`). Tests may need updates for new internal behavior but external contract is stable.

   **RESOLVED:** Plan 05-03 Task 2 keeps the public signature `async def execute(self, opportunity) -> bool` unchanged. `main.py` is untouched. `tests/test_executor.py` gets fixture-level updates in Plan 05-03 Task 3 (mock_opportunity now includes a non-empty path, mock_wallet now has public_key/private_key) but the three existing test names and their public-contract assertions are preserved.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | Runtime | ✓ | 3.14+ (project convention) | — |
| xrpl-py | Signing, submit, simulate helpers | ✓ | 4.5.0 | — |
| python-dotenv | `.env` loading | ✓ | (already in requirements.txt) | — |
| requests | HTTP RPC for simulate fallback | ✓ | (already in requirements.txt) | — |
| XRPL mainnet node `wss://s2.ripple.com` + `https://s2.ripple.com:51234` | Live + historical data | ✓ | — | `xrplcluster.com` (public alt) |
| Full-history node for replay test (2026-04-19 incident ~3 weeks back) | CURR / ATOM replay test | Probably ✓ on s2 | — | Use xrplcluster.com if s2 lacks |
| pytest + pytest-asyncio | Test harness | ✓ (existing) | — | — |

**Missing dependencies with no fallback:** none.

**Missing dependencies with fallback:** none — everything needed is already installed.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | (inferred — existing tests run via project pattern; no `pytest.ini` or `pyproject.toml` with pytest config verified — planner should check) |
| Quick run command | `pytest tests/test_executor.py -x` |
| Full suite command | `pytest tests/ -x` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ATOM-01 | Both legs fully built + signed before leg 1 submit | unit | `pytest tests/test_executor.py::test_atomic_both_legs_presigned_before_submit -x` | ❌ Wave 0 |
| ATOM-02 | Sequential N, N+1 assignment from single account_info | unit | `pytest tests/test_executor.py::test_atomic_sequences_are_n_and_n_plus_1 -x` | ❌ Wave 0 |
| ATOM-03 | Leg 2 submitted without waiting for leg 1 validation | unit (mock `submit`, assert no `tx`/validate call between) | `pytest tests/test_executor.py::test_atomic_leg2_submits_before_leg1_validates -x` | ❌ Wave 0 |
| ATOM-04 | Leg-1 terminal fail → sequence burn | unit (mock leg-1 → tecPATH_PARTIAL, assert burn submitted at Seq N+1) | `pytest tests/test_executor.py::test_leg1_terminal_fail_burns_sequence -x` | ❌ Wave 0 |
| ATOM-05 | Leg-2 fail after leg-1 commits → 2% recovery fires | integration (verify CircuitBreaker.record_trade called with negative profit) | `pytest tests/test_executor.py::test_leg2_fail_activates_existing_recovery -x` | ❌ Wave 0 |
| ATOM-06 | Single-writer guard | unit (mock concurrent submit attempt under asyncio.Lock) | `pytest tests/test_executor.py::test_single_writer_guard_rejects_concurrent -x` | ❌ Wave 0 |
| ATOM-07 | Leg-2 simulate terPRE_SEQ treated as pass | unit (mock sim → terPRE_SEQ, assert proceed to submit) | `pytest tests/test_executor.py::test_leg2_terPRE_SEQ_treated_as_pass -x` | ❌ Wave 0 |
| ATOM-08 | Decimal preserved throughout | unit (assert no float in tx_dict values, amounts) | `pytest tests/test_executor.py::test_atomic_all_amounts_are_decimal -x` | ❌ Wave 0 |
| ATOM-09 | Per-leg log entries w/ Seq, hash, engine_result, ledger_index | unit (mock log_trade, inspect calls) | `pytest tests/test_executor.py::test_atomic_per_leg_log_entries -x` | ❌ Wave 0 |
| ATOM-10 | No dead sequential-submit code | structural (grep: no orphaned old-path code; only one execute() impl) | `pytest tests/test_executor.py::test_no_dead_sequential_path -x` OR `ast`-based | ❌ Wave 0 |
| CURR-01 | HIGH_LIQ expanded to ≥6 currencies incl. SOLO + one more | unit (import config, assert len >= 6, assert "SOLO" in list) | `pytest tests/test_executor.py::test_high_liq_includes_solo_plus_one -x` | ❌ Wave 0 |
| CURR-02 | Env-change-only is sufficient to add a currency | unit (monkeypatch env, reload config, assert list changed) | `pytest tests/test_profit_math.py::test_high_liq_reloads_from_env -x` | ❌ Wave 0 |
| CURR-03 | Every HIGH_LIQ currency has documented issuer in `.env.example` | static check (read .env.example, assert each currency has a section block) | `pytest tests/test_config.py::test_env_example_documents_all_issuers -x` | ❌ Wave 0 |
| CLEAN-01 | LEG2_TIMEOUT_LEDGERS is imported and used | structural (grep + behavioral) | `pytest tests/test_executor.py::test_last_ledger_uses_env_var -x` | ❌ Wave 0 |
| CLEAN-02 | PROFIT_THRESHOLD_LOW_LIQ returned for non-HIGH_LIQ | unit (call get_profit_threshold("FAKE"), assert LOW_LIQ) | `pytest tests/test_profit_math.py::test_low_liq_returned_for_non_high_liq -x` | ❌ Wave 0 |
| **Replay** | 2026-04-19 4 failed trades would have succeeded under atomic | integration (real mainnet RPC → historical book_offers → simulated atomic math) | `pytest tests/test_replay.py -x --runslow` | ❌ Wave 0 |

### Sampling Rate (Nyquist)
- **Per task commit:** `pytest tests/test_executor.py -x` (fast, mocked, ~2s)
- **Per wave merge:** `pytest tests/ -x` (full suite, < 60s)
- **Phase gate:** Full suite green + replay test passes against live mainnet book data

### Wave 0 Gaps

All 17 tests in the mapping above are new. Group into three test files:

- [ ] `tests/test_executor.py` (extended) — covers ATOM-01 through ATOM-10, CLEAN-01
- [ ] `tests/test_profit_math.py` (extended) — covers CLEAN-02, CURR-02
- [ ] `tests/test_config.py` (NEW) — covers CURR-01, CURR-03
- [ ] `tests/test_replay.py` (NEW, marked `--runslow`) — covers 2026-04-19 replay scenario against live mainnet historical state
- [ ] `tests/conftest.py` (may need extension) — shared fixtures for mock simulate results, mock submit responses with provisional engine_results, incident tx hash fixtures
- [ ] No framework install required — pytest + pytest-asyncio already in use.

### Coverage Philosophy (Nyquist)
The 17 tests above cover:
- **Happy path** (both legs succeed) — proves the primary flow works
- **Leg-1 terminal fail** + burn — proves orphan handling closes replay door
- **Leg-2 fail after leg-1 commits** — proves existing recovery still fires
- **Leg-2 `terPRE_SEQ` sim** — proves state-dependent pass logic works
- **Decimal preservation** — proves SAFE-04 is not violated
- **Config-only currency add** — proves CURR-02 contract
- **Dead-knob wired** — proves CLEAN-01/02 actually took effect
- **Replay** — proves the architectural fix actually fixes the 2026-04-19 failure mode

This sample catches any architectural regression without requiring a combinatorial explosion. Missing from this mapping: fee-drift-between-legs (the user's decision is "uniform hardcoded fee," so there's no drift to test). If fees become dynamic later, add a test.

## Security Domain

> ASVS check-in — bot is a self-custody trading agent. Threat model is limited but non-trivial.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | N/A (no external API auth in atomic-submit path) | — |
| V3 Session Management | N/A | — |
| V4 Access Control | Yes (partial) | VPS `xrplbot` non-root user + systemd sandboxing (existing from Phase 4) |
| V5 Input Validation | Yes | Every value entering `tx_dict` must be Decimal-sourced. Simulate gate rejects malformed tx before submit. Existing `_build_tx_dict` pattern. |
| V6 Cryptography | Yes | `xrpl.core.keypairs.sign` — never hand-roll. Wallet seed stays in-process (T-01-10). |
| V7 Error Handling | Yes | Every submit wrapped in try/except. LIVE-03 requires full error logging. Burn failure must NOT mask leg-1 failure. |

### Known Threat Patterns for XRPL Signing

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Signed blob persists in memory / disk longer than intended | Information Disclosure | Keep `tx_blob` in local variables only; never persist to log file or `.jsonl`. Log the *hash*, not the blob. |
| Pre-signed leg 2 replayed after phase crash/restart | Tampering | Time-box via `LastLedgerSequence = current + LEG2_TIMEOUT_LEDGERS` (default 4 ≈ 20s). After expiry, the blob is definitively invalid [CITED: https://xrpl.org/docs/concepts/transactions/finality-of-results]. |
| Orphan Sequence consumed by attacker via seed compromise | Spoofing | Out of scope — seed compromise breaks the whole model. Focus on the narrower "accidental replay of a bot-generated blob" — burn pattern closes this. |
| Seed leaked via log file | Information Disclosure | Existing pattern: wallet seed read once in `config.py`; `Wallet.from_seed` in `main.py`; never logged. Verify this invariant still holds in atomic-submit rewrite. |
| Nonce reuse across legs (same Sequence) | Integrity | Strict sequential N, N+1 assertion. Tests cover this (ATOM-02). |
| Dust-filtered currency re-enters HIGH_LIQ list and causes bad fills | Denial of Service (self-inflicted) | Existing dust filter (PR #7) + profit threshold gate filter illiquid opportunities. Plan must run these checks against SOLO/USDT before marking CURR-01 done. |

## Sources

### Primary (HIGH confidence)

- **xrpl-py 4.5.0 source code** — `xrpl/asyncio/transaction/main.py` inspected directly in this environment via `inspect.getsource`. Confirmed: `submit()` uses `SubmitOnly`, `autofill()` preserves pre-set Sequence/Fee/LastLedgerSequence, `_LEDGER_OFFSET = 20`, `simulate()` rejects signed tx.
- **XRPL Sequence semantics** — [https://xrpl.org/docs/references/protocol/data-types/basic-data-types#account-sequence](https://xrpl.org/docs/references/protocol/data-types/basic-data-types) — "Whenever a transaction is included in a ledger, it uses up a sequence number ... regardless of whether the transaction executed successfully or failed with a tec-class error code. Other transaction failures don't get included in ledgers, so they don't change the sender's sequence number."
- **XRPL submit RPC** — [https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/submit](https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/submit) — confirmed submit-only mode returns after local node acceptance.
- **XRPL terPRE_SEQ** — [https://xrpl.org/docs/references/protocol/transactions/transaction-results/ter-codes](https://xrpl.org/docs/references/protocol/transactions/transaction-results/ter-codes) — "The Sequence number of the current transaction is higher than the current sequence number of the account sending it."
- **XRPL canceling a transaction** — [https://xrpl.org/docs/concepts/transactions/finality-of-results/canceling-a-transaction](https://xrpl.org/docs/concepts/transactions/finality-of-results/canceling-a-transaction) — no-op AccountSet pattern with matching Sequence.
- **XRPL finality of results** — [https://xrpl.org/docs/concepts/transactions/finality-of-results](https://xrpl.org/docs/concepts/transactions/finality-of-results) — LastLedgerSequence expiry semantics.
- **XRPL reliable transaction submission** — [https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission](https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission) — LastLedgerSequence = current + 4 for automated processes.
- **XRPL transaction cost** — [https://xrpl.org/docs/concepts/transactions/transaction-cost](https://xrpl.org/docs/concepts/transactions/transaction-cost) — load_factor can change between back-to-back calls; minimum fee 10 drops.
- **Sologenic SOLO issuer** — [https://xrpscan.com/account/rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz](https://xrpscan.com/account/rsoLo2S1kiGeCcn6hCUXVrCpGMWLrRrLZz) — canonical issuer address.
- **RLUSD issuer** — [https://xrpscan.com/token/RLUSD.rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De](https://xrpscan.com/token/RLUSD.rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De) — confirms `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`.

### Secondary (MEDIUM confidence)

- **XLS-0069 simulate method** — [https://medium.com/p6rkdoyeon/xls-69-secure-transaction-simulation-through-the-simulate-api-method-on-xrpl-14a3831c8b50](https://medium.com/p6rkdoyeon/xls-69-secure-transaction-simulation-through-the-simulate-api-method-on-xrpl-14a3831c8b50) — "simulate accepts unsigned tx_json or tx_blob, autofills Fee/Sequence/SigningPubKey/NetworkID only if absent."
- **XRPL simulate RPC reference** — [https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/simulate](https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/simulate) — confirmed by MEDIUM WebFetch (some 404 on deep-link variants).
- **XRPL full-history availability** — [https://xrpl.org/docs/concepts/networks-and-servers/ledger-history](https://xrpl.org/docs/concepts/networks-and-servers/ledger-history) — s2.ripple.com provides full history; backfill possible.

### Tertiary (LOW confidence)

- **XRPL top tokens by volume 2026** — multiple market trackers (xrpl.to, xrpscan.com/tokens, xpmarket.com) not directly fetched due to 403s. Recommendation to add SOLO + USDT is based on (a) they're already in project `setup_trust_lines.py` at Tiers 2-3, (b) SOLO is the memory-note recommendation, (c) USDT has GateHub backing. Confirm liquidity with a live `book_offers` call before marking CURR-01 complete.

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — xrpl-py 4.5 source read directly, version verified (`pip show`)
- XRPL protocol semantics (Sequence, submit, terPRE_SEQ, LastLedgerSequence): **HIGH** — official XRPL docs quoted directly
- Orphan handling pattern: **HIGH** — canonical AccountSet no-op explicitly documented on XRPL
- Fee determinism between back-to-back autofill: **MEDIUM** — load_factor *can* change but in practice rarely does in ~200ms; user-decided uniform hardcoded fee sidesteps the issue
- Currency recommendations: **MEDIUM** — SOLO confirmed as liquid via user memory + multiple sources; USDT is reasonable but less verified; planner should run a live `book_offers` spot-check
- Replay test viability: **MEDIUM** — `book_offers` with historical `ledger_index` documented as supported on full-history nodes, but haven't run it against s2.ripple.com in this environment
- Test architecture mapping: **HIGH** — all 17 tests have clear triggers and map 1:1 to REQ IDs

**Research date:** 2026-04-20
**Valid until:** 2026-05-20 (30 days; XRPL protocol and xrpl-py API are stable)
**Revised 2026-04-20** for plan-checker Warning 6 / Dimension 11 — Open Questions section renamed to `## Open Questions (RESOLVED)` with each of the 6 questions carrying an inline `RESOLVED:` marker pointing to the plan + task that answers it.
