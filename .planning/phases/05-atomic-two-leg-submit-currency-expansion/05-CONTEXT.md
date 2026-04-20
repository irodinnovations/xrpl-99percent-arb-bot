# Phase 5: Atomic Two-Leg Submit + Currency Expansion — Context

**Gathered:** 2026-04-20
**Status:** Ready for research & planning
**Source:** Live-trade incident analysis (2026-04-19) + scanner health test (2026-04-20) captured in `memory/project_two_leg_rewrite.md`

<domain>
## Phase Boundary

This phase delivers three tightly-coupled changes to the execution path:

1. **Atomic two-leg submission** — pre-sign both legs of an arbitrage trade up front with sequential `Sequence` numbers (N, N+1) and submit them back-to-back so they apply in the same or adjacent ledger. Replaces the current sequential flow where leg 2 is built and signed AFTER leg 1 commits, which opens a 5-7s drift window.
2. **HIGH_LIQ_CURRENCIES expansion** — extend the scanner's route universe beyond the current `USD,USDC,RLUSD,EUR` set to capture more opportunities during calm markets. Pure `.env` config change, no code changes required.
3. **Dead-knob cleanup** — two env vars (`LEG2_TIMEOUT_LEDGERS`, `PROFIT_THRESHOLD_LOW_LIQ`) are defined in `src/config.py` but not wired into the code paths that would use them. Each must be either wired in or removed.

**In scope:**
- Rewrite of `src/executor.py` execution path for DRY_RUN=False
- Pre-sim of both legs before leg 1 submission
- Sequence reservation + orphan-handling for leg-1-fail
- Post-trade logging updates for both-leg audit trail
- `HIGH_LIQ_CURRENCIES` env expansion + issuer mapping in `.env.example`
- Config cleanup for `LEG2_TIMEOUT_LEDGERS` and `PROFIT_THRESHOLD_LOW_LIQ`
- New test coverage for all atomic-submit scenarios

**Out of scope:**
- Dynamic Sequence reservation for >2 legs (triangular arb is deferred)
- Fee-market / priority-fee strategies beyond existing autofill defaults
- Wallet multi-writer support (still single-writer only)
- Changes to scanner / pathfinder / profit-math — atomic submit is purely an execution-path change
- New currency pair research beyond "add ≥2 more to HIGH_LIQ list"
- **Per-leg path splitting** (see Deferred Concerns below) — v1 uses the same `opportunity.paths` for both legs and relies on the atomic window + `tfPartialPayment` flag

</domain>

<decisions>
## Implementation Decisions

### Architecture — Atomic Submit
- **Pre-sign both legs before leg 1 network submit.** Leg 1 uses current Sequence N; leg 2 uses N+1. Both `LastLedgerSequence` windows aligned (same or adjacent ledger).
- **Submission order:** leg 1 submitted first, leg 2 submitted immediately after leg 1's submit call returns (do NOT wait for leg 1 validation). Submit-only (no validation wait) for leg 2 keeps the gap to network-round-trip latency.
- **Simulate both legs before ANY submission.** Leg 1 must return `tesSUCCESS`. Leg 2 is simulated at the CURRENT ledger state (pre-leg-1) and `terPRE_SEQ` is treated as pass (state-dependent — already handled this way in current code per memory notes).
- **Atomic submit is the only live path.** Previous sequential-submit path is REMOVED from `src/executor.py`. No feature flag, no fallback — this is a forward migration. DRY_RUN mode continues to log without submitting.
- **Both legs share the SAME `opportunity.paths` array in v1.** See "Deferred Concerns" — per-leg path splitting is out of scope for this phase. The atomic window (100-200ms vs old 5-7s) is the empirical safety margin. Each leg log captures the actual `path_used` field so post-deploy data can distinguish atomic-window failures from path-split failures if leg 2 ever hits `tecPATH_PARTIAL`.

### Leg-1-Fail / Orphan Handling
- **If leg 1 fails terminally (tec*/tef*/tem*):** the pre-signed leg 2 must NOT be replayable later. Options researcher should evaluate:
  1. Burn Sequence N+1 via a no-op AccountSet tx (costs one fee drop, guarantees no replay)
  2. Let `LastLedgerSequence` expire naturally (no extra tx, but leg 2 is a valid signed blob on disk/logs until expiry)
  3. Wait-and-cancel via `OfferCancel` if leg 2 is an Offer-type tx
- **Research decision:** pick one approach, justify tradeoff (tx cost vs. replay-safety vs. simplicity). Memory notes default preference toward burn-via-no-op for determinism.

### Leg-2-Fail-After-Leg-1-Commits
- **Existing 2% market-dump recovery flow is preserved as-is.** Atomic submit reduces the LIKELIHOOD of this failure mode; it does not eliminate it. Recovery flow must still work.

### Fee Strategy
- **Both legs signed with the same fee (autofill default).** No leg-1 priority escalation in v1. Researcher should note if autofill can produce DIFFERENT fees for the two legs (possible if network conditions shift between two autofill calls) and recommend whether to force uniform fee.

### Single-Writer Guard
- **Pre-submit check that no other tx is pending from this account during the arb window.** Currently implicit (arb loop is single-threaded per account); make explicit assertion so future concurrent code additions don't silently break atomicity.

### Currency Expansion
- **Minimum 2 additional currencies added beyond USD/USDC/RLUSD/EUR.** Researcher identifies candidates from XRPL liquid-issuer ecosystem (SOLO is a strong candidate per memory). Each new currency needs a trusted issuer address documented in `.env.example`.
- **`HIGH_LIQ_CURRENCIES` stays a comma-separated env string.** No YAML/JSON config file. Restart required to pick up changes (acceptable — aligns with current pattern).

### Dead-Knob Resolution
- **`LEG2_TIMEOUT_LEDGERS`** — currently defined in `src/config.py` but NOT imported into `src/executor.py`. The actual ledger window is `current_ledger + 4` inline at `src/executor.py:180`. Decision: wire `LEG2_TIMEOUT_LEDGERS` into the atomic-submit path as the `LastLedgerSequence` offset for BOTH legs (replaces the inline `+ 4`). This gives the phase a consistent, configurable window aligned with the new architecture.
- **`PROFIT_THRESHOLD_LOW_LIQ`** — defined in `src/config.py`, imported in `src/profit_math.py:9`, but `get_profit_threshold()` only returns `PROFIT_THRESHOLD_HIGH_LIQ` or the base `PROFIT_THRESHOLD`. Decision: wire it in properly so non-HIGH_LIQ currencies return the LOW_LIQ threshold. This creates a true three-tier threshold model (HIGH_LIQ → LOW_LIQ → base fallback) and preserves the env-var semantics that existing users may have configured.

### Testing
- **Must add scenarios:** both-legs-succeed, leg-1-fails-leg-2-sequence-burned, leg-2-fails-after-leg-1-commits (verifies existing 2% recovery still fires), simulate-leg-2-returns-terPRE_SEQ-treated-as-pass, fee-mismatch-between-legs-if-autofill-diverges.
- **ATOM-01 covered by TWO narrow tests** (per plan-checker Warning 3 revision 2026-04-20):
  1. `test_both_legs_simulated_before_first_submit` — asserts both simulate RPC calls complete before any submit RPC call
  2. `test_both_legs_signed_before_first_submit` — spies on `keypairs_sign` and asserts both leg signings complete before any submit
  Two narrowly-named tests are more diagnostic when one breaks than one broadly-named test.
- **Replay test:** parameterize a test against the 4 failed trade hashes from 2026-04-19 (2EBD65E8, E8A24309, 1C63E5763115D09F, D6B62B3121F56901). Using recorded book state at those ledgers, verify atomic submit would have succeeded. This is the empirical proof the architecture works for the known failure cases.
- **Existing 194 tests must still pass.**

### Logging
- **Per-leg log entries** with: leg number (1 or 2), Sequence, hash, engine_result, ledger_index, latency-from-leg1-submit (for leg 2), **`path_used`** (the actual Paths array submitted for that leg — diagnostic field for shared-paths failure mode analysis).
- **Trade-level summary entry** logged on completion with both legs' outcomes aggregated (for dashboard + backtester consumption).
- **Log schema change is ADDITIVE** — existing fields preserved, new fields added. Dashboard + backtester should continue to work during transition.

### Claude's Discretion
- Exact function signatures and method names in `src/executor.py`
- Internal structure (whether to add a new `AtomicExecutor` class or refactor in-place)
- Specific issuer addresses for new currencies (research-driven)
- Test file naming convention (follow existing patterns in `tests/`)
- Whether to emit new Telegram alerts for leg-1-fail vs. leg-2-fail cases (default: yes, to distinguish failure modes in ops visibility)

</decisions>

<deferred_concerns>
## Deferred Concerns — Acknowledged but NOT Fixed in Phase 5

### Per-leg path splitting (plan-checker Warning 5, 2026-04-20)

**Concern:** Both legs pass the FULL `opportunity.paths` array to the Payment's `Paths` field. Research states the "correct" architecture splits paths — leg 1 uses the XRP→IOU portion, leg 2 uses the IOU→XRP portion. Risk: if leg 1 consumes liquidity at a specific order book level, leg 2 pointing at the SAME path set could hit `tecPATH_PARTIAL` even within the 100-200ms atomic window.

**Why we're NOT fixing this in Phase 5:**
1. **Empirical safety margin.** The atomic window (100-200ms) is ~30-50x smaller than the 5-7s window that caused the 2026-04-19 incident. Liquidity that clears within 100-200ms of a leg-1 fill is rare in practice for the HIGH_LIQ currencies we trade.
2. **`tfPartialPayment` fallback.** The node picks a working subset of the provided paths per leg independently — if leg 1 consumed path A, leg 2 can still route through path B if both are in the shared array.
3. **Pathfinder scope creep.** Splitting paths per-leg requires changes to `src/pathfinder.py` (structured XRP→IOU and IOU→XRP segments) that are out of Phase 5's execution-path-only boundary.
4. **Data-driven escalation.** We want to collect real data before investing in path-split infra. The `path_used` log field (Plan 05-03 Task 1) captures the actual Paths array for every submitted leg. If post-deploy data shows `tecPATH_PARTIAL` on leg 2 with `latency_from_leg1_ms < 500`, that is the signal to escalate to per-leg path splitting in a future phase.

**Mitigation for the risk window:**
- `log_trade_leg(path_used=...)` records the Paths field on every leg (see 05-03-PLAN Task 1 + Task 2)
- `log_trade_leg(latency_from_leg1_ms=...)` records the inter-leg latency
- Post-deploy, operator can query the JSONL log for `entry_type=leg AND leg=2 AND engine_result=tecPATH_PARTIAL` to see how often (if ever) this failure mode occurs
- 2% market-dump recovery (CircuitBreaker) still fires if leg 2 fails after leg 1 commits — user loses only 2% on such trades (bounded)

**Tracking:** See `05-03-PLAN.md` T-05-20 in threat register + `05-VALIDATION.md` "Manual-Only Verifications" section for the post-deploy monitoring instructions.

**Future phase trigger:** if, during the first 30 days of live trading, > 3 trades fail with `tecPATH_PARTIAL` on leg 2 AND `latency_from_leg1_ms < 500`, open a phase for per-leg path splitting.
</deferred_concerns>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Memory & Incident Context
- `memory/project_two_leg_rewrite.md` — 2026-04-19 live-trade incident details (4 failed trades, tecPATH_PARTIAL, ~5-7s inter-leg drift), 2026-04-20 scanner health test findings (0 opps in 15h at 0.3% = calm market), dead-knob inventory, VPS state snapshot. **This is the primary context for WHY this phase exists.**

### Project Conventions
- `CLAUDE.md` (project root) — project instructions, constraints (1 CPU / 4GB VPS, `decimal.Decimal` only, minimal deps)
- `.planning/REQUIREMENTS.md` — REQ-IDs for this phase: ATOM-01 through ATOM-10, CURR-01 through CURR-03, CLEAN-01, CLEAN-02
- `.planning/ROADMAP.md` — Phase 5 section with success criteria

### Source Code — Execution Path (must read before modifying)
- `src/executor.py` — CURRENT sequential execute flow (contains the 5-7s drift bug). Primary file being rewritten.
- `src/simulator.py` — `simulate` RPC wrapper used for both legs' pre-submit validation. See PR #15 for the `engine_result` field fix (live on VPS).
- `src/config.py` — env var definitions (HIGH_LIQ_CURRENCIES, LEG2_TIMEOUT_LEDGERS, PROFIT_THRESHOLD_HIGH_LIQ, PROFIT_THRESHOLD_LOW_LIQ, SLIPPAGE_BASE, MAX_TRADE_XRP_ABS)
- `src/profit_math.py` — `get_profit_threshold()` function at top, currently missing the LOW_LIQ branch (CLEAN-02)
- `src/connection.py` — XRPL WebSocket client, used for submit and for fetching account Sequence/ledger
- `src/trade_logger.py` — JSONL log writer; per-leg log entries and trade-level summary must flow through here
- `src/safety.py` — existing 2% market-dump / circuit breaker logic that must remain untouched but must still activate on leg-2-fail-after-leg-1-commits

### Config Files
- `.env.example` — documents every env var; must be updated for HIGH_LIQ_CURRENCIES expansion + issuer docs + LEG2_TIMEOUT_LEDGERS resolution
- `.planning/REQUIREMENTS.md` — the 15 REQ-IDs this phase must address

### Prior PRs (context for existing behavior)
- PR #15 — `engine_result` simulate fix (live 2026-04-17) — leg-2 simulation reads engine_result correctly now
- PR #16 — preflight check script — may need extension for atomic-submit readiness
- PR #17 — volatility parser — unrelated but recent work on `src/volatility.py`

### External XRPL Docs (researcher should consult)
- XRPL Sequence field semantics: https://xrpl.org/basic-data-types.html#account-sequence
- Submit-only vs submit-and-wait: https://xrpl.org/submit.html
- `terPRE_SEQ` result code: https://xrpl.org/tec-codes.html
- `tecPATH_PARTIAL`: the failure mode of the 2026-04-19 incident

</canonical_refs>

<specifics>
## Specific Ideas

### Hard Data Points
- **Failure rate before fix:** 4/4 live trades on 2026-04-19 hit `tecPATH_PARTIAL` on leg 2. Inter-leg delay was 5-7s.
- **Paper-trading result at 0.3% threshold, 2026-04-20:** 0 opportunities in 15h of continuous scanning → market is genuinely calm, architectural fix is only path to capturing rare volatility bursts.
- **Current HIGH_LIQ list:** `USD,USDC,RLUSD,EUR` (4 currencies). Proposed additions: SOLO + at least one more (researcher picks).
- **Loss bounds from 2026-04-19:** per-trade losses were 0.025-0.029 XRP via 2% market-dump recovery. Net session loss: -0.121040 XRP + 0.1512 USD residual (since cleared).
- **Branch:** `claude/two-leg-rewrite` @ `0f28bdb` (draft PR #18). Worktree is `claude/awesome-shamir-12ae68`. 194 tests passing on the rewrite branch.

### Reference Implementation Patterns to Study
- Current `execute()` at `src/executor.py:95` — the sequential flow being replaced
- Current `LastLedgerSequence` window logic at `src/executor.py:180` (hardcoded `+ 4`)
- Current `get_profit_threshold()` at `src/profit_math.py:82` — the branch that needs LOW_LIQ wired in
- Existing simulate wrapper — both legs will call it back-to-back before leg 1 submit

### Validation Gates the Plan Must Include
- Plan verification by `gsd-plan-checker` must confirm: every ATOM/CURR/CLEAN REQ-ID is addressed by at least one PLAN.md
- Every task must list the files it modifies (especially for `src/executor.py` which is the core change)
- Orphan-handling approach must be named explicitly in the plan (not left as "TBD during execution")

</specifics>

<deferred>
## Deferred Ideas

### Not in Phase 5 (future work)
- **Triangular / 3-leg arbitrage** — architecture supports 2-leg only; N-leg generalization is future work
- **Dynamic priority-fee escalation** — uniform autofill fee for v1 (research may recommend, but implementation deferred)
- **Multi-wallet / multi-account support** — still single-writer
- **Automatic volatility-based threshold adjustment** — AI brain still advisory-only (no auto-threshold changes)
- **Dashboard UI changes for per-leg visibility** — log schema is additive so dashboard keeps working, but richer UI is future Phase 6+
- **Backtester updates for atomic-submit semantics** — backtester currently replays sequential execution; updating it to reflect atomic semantics is a follow-up (out of Phase 5 scope unless trivial)
- **Per-leg path splitting** — see `<deferred_concerns>` above. Logged `path_used` field provides the data to decide whether to invest in this in a future phase.

### Research-Gated Items
- Final list of added currencies (CURR-01) — researcher recommends ≥2 specific candidates with issuer addresses; if no strong candidates exist beyond SOLO, a one-currency expansion is acceptable with documented rationale

</deferred>

---

*Phase: 05-atomic-two-leg-submit-currency-expansion*
*Context gathered: 2026-04-20 from memory/project_two_leg_rewrite.md + live code inspection*
*Revised 2026-04-20 for plan-checker Warning 5 — added `<deferred_concerns>` acknowledging shared-paths as deferred, plus the `path_used` log field as the post-deploy diagnostic hook.*
