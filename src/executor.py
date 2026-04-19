"""Trade executor — two-leg Payment arbitrage with pre-sim gates.

Background
----------
The XRPL protocol rejects a single Payment where source and destination are
both XRP native with SendMax/Paths/tfPartialPayment (rippled Payment.cpp).
The original atomic XRP->IOU->XRP design returned temBAD_SEND_XRP_MAX on
every submit. Every arbitrage is therefore executed as two sequential
Payments, each of which has at least one IOU side and is protocol-legal.

See docs/two_leg_architecture.md for the full rationale and failure-mode
tables.

Flow
----
1. Safety checks (circuit breaker, blacklist) — skip if halted.
2. Skip opportunities that lack two-leg metadata (legacy multi-hop).
3. Autofill: fetch account Sequence and current ledger from node.
4. Build leg 1 (buy IOU with XRP) and pre-simulate. Abort if not tesSUCCESS.
5. Extract delivered IOU amount from leg 1's simulate result. Use that value
   (not the opportunity's theoretical iou_amount) to parameterize leg 2.
6. Build leg 2 (sell IOU for XRP) with Sequence+1 and pre-simulate.
   Abort if not tesSUCCESS.
7. DRY_RUN: log both legs with their sim results, record paper trade, exit.
8. LIVE: submit leg 1, wait for validation, re-fetch current ledger, submit
   leg 2 with fresh LastLedgerSequence, wait for validation, record P&L.
9. If leg 1 validated but leg 2 did not: invoke recovery (stubbed in B2).

Key safety invariants
---------------------
- LIVE-01: Both legs must pass simulate (tesSUCCESS) before any submission.
- LIVE-02: DRY_RUN=True never submits any transaction.
- LIVE-03: Every failed submission is logged with the engine_result and raw.
- Field ordering in the tx dict is logical-readable only; XRPL's binary
  codec sorts fields by their internal field code for signing.
- tfPartialPayment is NEVER set on either leg. Without it, Amount is the
  exact required delivery. This means any leg either delivers the full
  target or fails cleanly — no half-partial states to reason about.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from xrpl.core.binarycodec import encode as xrpl_encode, encode_for_signing
from xrpl.core.keypairs import sign as keypairs_sign
from xrpl.wallet import Wallet

from src.config import (
    XRPL_RPC_URL,
    DRY_RUN,
    MAX_TRADE_XRP_ABS,
    MIN_BALANCE_GUARD_PCT,
    LEG2_RETRY_MAX,
    RECOVERY_MAX_LOSS_PCT,
    MID_TRADE_HALT_HOURS,
)
from src.pathfinder import Opportunity
from src.simulator import (
    simulate_transaction,
    simulate_transaction_ws,
    SimResult,
    HttpRpcClient,
    extract_delivered_iou,
)
from src.safety import CircuitBreaker, Blacklist
from src.trade_logger import log_trade
from src.telegram_alerts import send_alert

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# Leg 1 SendMax is XRP drops; we allow up to this much overspend vs the
# theoretical input_xrp to absorb minor book movement between pathfinder
# and simulate. If sim still passes at this cap, the route is executable.
_LEG1_SENDMAX_BUFFER = Decimal("0.01")  # 1%

# LastLedgerSequence window — how many ledgers the tx remains valid before
# rippled rejects it with tefMAX_LEDGER. 4 ledgers ≈ 16-20 seconds on
# mainnet, long enough to clear congestion but tight enough that a stuck
# tx can never be replayed later at a wildly different price.
_LEDGER_WINDOW = 4

# Hard cap on polling loop for transaction validation. If the ledger
# advances past (LastLedgerSequence + _LEDGER_POLL_GRACE) without the tx
# being seen, we treat it as lost.
_LEDGER_POLL_GRACE = 2

# Poll interval (seconds) while waiting for a transaction to validate.
_TX_POLL_INTERVAL = 1.5

# Standard XRPL reference fee (12 drops).
_STANDARD_FEE_DROPS = "12"

# tfPartialPayment flag. NEVER set on leg 1 or leg 2 of a normal arb
# (xrpDirect forbids it). Only used on startup IOU drain where we just
# want to accept whatever XRP the market gives for our held balance.
_TF_PARTIAL_PAYMENT = 131072

# Max attempts in the recovery flow for the emergency IOU market-dump.
# After this many fails, the bot halts for MID_TRADE_HALT_HOURS and
# blacklists the route. Held IOU cleans up via the startup drain guard.
_RECOVERY_DUMP_ATTEMPTS = 2

# Startup drain XRP ceiling (in drops). With tfPartialPayment, Amount
# is an upper bound; we set it unreasonably high (1B drops = 1000 XRP)
# so the market delivers whatever it can for our SendMax IOU without
# the ceiling ever binding.
_DRAIN_XRP_CEILING_DROPS = "1000000000"


# ---------------------------------------------------------------------------
# Module-level transaction builders
# ---------------------------------------------------------------------------


def _format_iou_value(value: Decimal) -> str:
    """Serialize a Decimal IOU value the way XRPL JSON-RPC expects.

    No scientific notation, no spurious trailing zeros, but significant
    trailing zeros on integers are preserved (100 stays 100, not 1).
    """
    s = f"{value:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _build_leg1_tx(
    wallet_address: str,
    opportunity: Opportunity,
    sendmax_buffer: Decimal = _LEG1_SENDMAX_BUFFER,
) -> dict:
    """Leg 1: spend XRP to acquire IOU (xrpDirect=false, protocol-legal).

    Shape per docs/two_leg_architecture.md:
      - Amount: IOU dict (currency/issuer/value) — exact target delivery
      - SendMax: XRP drops as string, input_xrp * (1 + buffer)
      - Destination: same as Account (self-payment, typical for arb)
      - No Paths — rippled's default pathfinding walks the XRP/IOU book
      - No Flags — tfPartialPayment is explicitly forbidden here

    The SendMax buffer tolerates minor book drift between when the scanner
    built the opportunity and when rippled runs simulate. If sim passes at
    this cap, the trade is executable within the overspend budget.
    """
    if not opportunity.iou_currency or not opportunity.buy_issuer:
        raise ValueError(
            "Leg 1 requires iou_currency and buy_issuer on the Opportunity "
            "(legacy multi-hop opportunities are not supported by the "
            "two-leg executor)"
        )
    if opportunity.iou_amount <= Decimal("0"):
        raise ValueError("Leg 1 requires a positive iou_amount on the Opportunity")
    if opportunity.input_xrp <= Decimal("0"):
        raise ValueError("Leg 1 requires a positive input_xrp on the Opportunity")

    send_max_drops = int(
        opportunity.input_xrp * DROPS_PER_XRP * (Decimal("1") + sendmax_buffer)
    )

    return {
        "TransactionType": "Payment",
        "Account": wallet_address,
        "Destination": wallet_address,
        "Amount": {
            "currency": opportunity.iou_currency,
            "issuer": opportunity.buy_issuer,
            "value": _format_iou_value(opportunity.iou_amount),
        },
        "SendMax": str(send_max_drops),
    }


def _build_leg2_tx(
    wallet_address: str,
    opportunity: Opportunity,
    iou_amount_to_sell: Decimal,
) -> dict:
    """Leg 2: spend held IOU to receive XRP (xrpDirect=false, protocol-legal).

    Shape per docs/two_leg_architecture.md:
      - Amount: XRP drops as string — exact required delivery
      - SendMax: IOU dict matching the amount actually delivered by leg 1
      - Paths: only for cross-issuer arb, routes through sell_issuer's book.
        Same-issuer arb relies on rippled's default pathfinding.
      - No Flags — tfPartialPayment is explicitly forbidden here

    `iou_amount_to_sell` comes from leg 1's simulate delivered_amount, not
    from opportunity.iou_amount. The theoretical value in the Opportunity
    can drift slightly from what leg 1 actually delivered (book walk
    precision, rippled's internal rounding), so leg 2 must be built from
    the concrete post-leg-1 number to stay self-consistent.
    """
    if not opportunity.iou_currency or not opportunity.buy_issuer:
        raise ValueError("Leg 2 requires iou_currency and buy_issuer on the Opportunity")
    if iou_amount_to_sell <= Decimal("0"):
        raise ValueError("Leg 2 requires a positive iou_amount_to_sell")
    if opportunity.output_xrp <= Decimal("0"):
        raise ValueError("Leg 2 requires a positive output_xrp on the Opportunity")

    output_drops = int(opportunity.output_xrp * DROPS_PER_XRP)
    sell_issuer = opportunity.sell_issuer or opportunity.buy_issuer

    tx: dict = {
        "TransactionType": "Payment",
        "Account": wallet_address,
        "Destination": wallet_address,
        "Amount": str(output_drops),
        "SendMax": {
            "currency": opportunity.iou_currency,
            "issuer": opportunity.buy_issuer,
            "value": _format_iou_value(iou_amount_to_sell),
        },
    }

    # Cross-issuer arb explicitly routes through the rich side's book.
    # Same-issuer arb leaves it to rippled's default pathfinding.
    if sell_issuer != opportunity.buy_issuer:
        tx["Paths"] = [[{
            "currency": opportunity.iou_currency,
            "issuer": sell_issuer,
            "type": 48,
            "type_hex": "0000000000000030",
        }]]

    return tx


# Backwards-compat alias: `extract_delivered_iou` moved to simulator.py in B3
# because that's where simulate-response parsing belongs. Executor tests and
# any external caller can still import _extract_delivered_iou from here.
def _extract_delivered_iou(sim_raw: Optional[dict]) -> Optional[Decimal]:
    """Convenience: pull delivered IOU value out of a raw sim response dict.

    Prefer `SimResult.delivered_iou_value()` in new code — this helper is
    kept so tests that accept a raw dict don't have to construct SimResult.
    """
    if not sim_raw:
        return None
    meta = sim_raw.get("meta") or {}
    return extract_delivered_iou(meta.get("delivered_amount"))


def _build_market_dump_tx(
    wallet_address: str,
    opportunity: Opportunity,
    iou_held: Decimal,
    max_loss_pct: Decimal = RECOVERY_MAX_LOSS_PCT,
) -> dict:
    """Emergency IOU->XRP dump used when leg 2 retries are exhausted.

    Atomic floor shape (no tfPartialPayment):
      - Amount:  XRP drops — minimum acceptable return, derived from the
        original leg-1 input_xrp and a hard max_loss_pct. If the market
        can't deliver at least this, the tx fails cleanly and we halt.
      - SendMax: IOU dict for every unit of held IOU — the cap on what
        we're willing to spend. rippled will usually spend less if rates
        are favorable.
      - Paths:   cross-issuer routes through sell_issuer. Same-issuer
        omits Paths and relies on default pathfinding.

    Not to be confused with _build_startup_drain_tx, which has no floor
    because the original entry price isn't known at startup.
    """
    if iou_held <= Decimal("0"):
        raise ValueError("Market-dump requires a positive iou_held")

    min_xrp_drops = int(
        opportunity.input_xrp * (Decimal("1") - max_loss_pct) * DROPS_PER_XRP
    )
    if min_xrp_drops <= 0:
        raise ValueError("Market-dump floor must be positive drops")

    sell_issuer = opportunity.sell_issuer or opportunity.buy_issuer
    tx: dict = {
        "TransactionType": "Payment",
        "Account": wallet_address,
        "Destination": wallet_address,
        "Amount": str(min_xrp_drops),
        "SendMax": {
            "currency": opportunity.iou_currency,
            "issuer": opportunity.buy_issuer,
            "value": _format_iou_value(iou_held),
        },
    }
    if sell_issuer != opportunity.buy_issuer:
        tx["Paths"] = [[{
            "currency": opportunity.iou_currency,
            "issuer": sell_issuer,
            "type": 48,
            "type_hex": "0000000000000030",
        }]]
    return tx


def _build_startup_drain_tx(
    wallet_address: str,
    currency: str,
    issuer: str,
    iou_balance: Decimal,
) -> dict:
    """Startup IOU drain with tfPartialPayment — accept any positive XRP.

    Used by the bot-startup recovery guard when a held IOU is found on
    boot with no known entry-price context. tfPartialPayment is LEGAL
    here because source is IOU (xrpDirect=false); Amount becomes a
    ceiling and the market delivers whatever it can.

    Because no original opportunity exists, there is no safety floor —
    this is a 'clean wallet' operation, not a profit operation. The
    caller must only invoke this on genuine leftovers.
    """
    if iou_balance <= Decimal("0"):
        raise ValueError("Startup drain requires a positive iou_balance")

    return {
        "TransactionType": "Payment",
        "Account": wallet_address,
        "Destination": wallet_address,
        "Amount": _DRAIN_XRP_CEILING_DROPS,
        "SendMax": {
            "currency": currency,
            "issuer": issuer,
            "value": _format_iou_value(iou_balance),
        },
        "Flags": _TF_PARTIAL_PAYMENT,
    }


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class TradeExecutor:
    """Execute arbitrage opportunities as two sequential Payments.

    Each execute() call either:
      - skips (safety gate or missing two-leg metadata),
      - paper-trades (DRY_RUN: logs both legs with sim results), or
      - live-trades (LIVE: submits both legs, records P&L, handles recovery).

    Recovery is stubbed in phase B2; phase C replaces `_recover` with the
    full retry / market-dump / halt-and-blacklist flow.
    """

    def __init__(
        self,
        wallet: Wallet,
        circuit_breaker: CircuitBreaker,
        blacklist: Blacklist,
        rpc_client: Optional[HttpRpcClient] = None,
        connection=None,
        dry_run: bool = DRY_RUN,
    ):
        self.wallet = wallet
        self.circuit_breaker = circuit_breaker
        self.blacklist = blacklist
        self.rpc_client = rpc_client or HttpRpcClient(XRPL_RPC_URL)
        self.connection = connection
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def execute(
        self,
        opportunity: Opportunity,
        current_balance: Optional[Decimal] = None,
    ) -> bool:
        """Run an opportunity through the two-leg pipeline.

        `current_balance` is used by the MIN_BALANCE_GUARD_PCT check to
        short-circuit all trading if the account balance has drifted below
        a safe fraction of the circuit breaker's reference balance. Callers
        that already have a fresh balance reading should pass it; older
        call sites can omit it and the guard is skipped.

        Returns True when a paper or live trade was recorded, False when
        the opportunity was skipped or rejected.
        """
        # ---- safety gates ----------------------------------------------------
        if self.circuit_breaker.is_halted():
            logger.warning("Circuit breaker HALTED — skipping trade")
            return False

        if self.blacklist.is_blacklisted(opportunity.paths):
            logger.warning("Path is blacklisted — skipping trade")
            return False

        # Time-expiring route block (populated by recovery flow + sim
        # failure counter). Auto-clears after ROUTE_BLACKLIST_HOURS.
        if self.blacklist.is_route_blocked(opportunity.route_key()):
            logger.warning(
                f"Route {opportunity.route_key()} is time-blacklisted — skipping"
            )
            return False

        # Defensive skip for any opportunity missing two-leg metadata.
        # B4 removed multi-hop emission, but this guard keeps the executor
        # safe if a caller ever hand-builds an Opportunity without the
        # required fields.
        if not opportunity.iou_currency or not opportunity.buy_issuer:
            logger.info(
                "Skipping opportunity without two-leg metadata "
                "(iou_currency/buy_issuer unset)"
            )
            return False

        # ---- balance guards --------------------------------------------------
        # Absolute cap — defense against balance-calculation bugs in the
        # pathfinder's position sizing.
        if opportunity.input_xrp > MAX_TRADE_XRP_ABS:
            logger.warning(
                f"Trade size {opportunity.input_xrp} XRP exceeds "
                f"MAX_TRADE_XRP_ABS {MAX_TRADE_XRP_ABS} — skipping"
            )
            return False

        # Percentage guard — skip if balance has drifted below the
        # reference floor. Only enforced when caller supplies a balance
        # and the circuit breaker has a reference snapshot.
        if current_balance is not None:
            ref = self.circuit_breaker.reference_balance
            if ref > Decimal("0"):
                ratio = current_balance / ref
                if ratio < MIN_BALANCE_GUARD_PCT:
                    logger.critical(
                        f"Balance guard tripped: current {current_balance} XRP "
                        f"is {ratio:.4f} of reference {ref} XRP "
                        f"(floor {MIN_BALANCE_GUARD_PCT}) — halting trade"
                    )
                    return False

        # ---- autofill: Sequence + current ledger -----------------------------
        account_info = await self._fetch_account_info()
        if account_info is None:
            return False
        sequence, current_ledger = account_info

        # ---- build + pre-sim leg 1 ------------------------------------------
        try:
            leg1_tx = _build_leg1_tx(self.wallet.address, opportunity)
        except ValueError as e:
            logger.warning(f"Leg 1 build failed: {e}")
            return False

        leg1_tx["Sequence"] = sequence
        leg1_tx["Fee"] = _STANDARD_FEE_DROPS
        leg1_tx["LastLedgerSequence"] = current_ledger + _LEDGER_WINDOW

        leg1_sim = await self._simulate(leg1_tx)
        if not leg1_sim.success:
            detail = f" | detail: {leg1_sim.error}" if leg1_sim.error else ""
            logger.warning(
                f"Leg 1 simulation FAILED ({leg1_sim.result_code}) — "
                f"route rejected, no state acquired{detail}"
            )
            self.blacklist.record_sim_failure(opportunity.route_key())
            return False

        iou_delivered = leg1_sim.delivered_iou_value()
        if iou_delivered is None:
            logger.warning(
                "Leg 1 simulation reported no delivered_amount — "
                "cannot parameterize leg 2 safely, skipping"
            )
            return False

        # ---- build + pre-sim leg 2 ------------------------------------------
        try:
            leg2_tx = _build_leg2_tx(self.wallet.address, opportunity, iou_delivered)
        except ValueError as e:
            logger.warning(f"Leg 2 build failed: {e}")
            return False

        leg2_tx["Sequence"] = sequence + 1
        leg2_tx["Fee"] = _STANDARD_FEE_DROPS
        leg2_tx["LastLedgerSequence"] = current_ledger + _LEDGER_WINDOW

        leg2_sim = await self._simulate(leg2_tx)
        if not leg2_sim.success:
            # Leg 2 sim can legitimately fail on mainnet today because the
            # account does not yet hold the IOU that leg 1 would deliver.
            # Treat this as a skip — phase D decides whether to relax the
            # gate empirically.
            detail = f" | detail: {leg2_sim.error}" if leg2_sim.error else ""
            logger.warning(
                f"Leg 2 simulation FAILED ({leg2_sim.result_code}) — "
                f"route rejected, no state acquired{detail}"
            )
            self.blacklist.record_sim_failure(opportunity.route_key())
            return False

        # ---- paper / live branch --------------------------------------------
        base_trade_data = self._build_trade_metadata(
            opportunity, iou_delivered, leg1_sim, leg2_sim
        )

        if self.dry_run:
            return await self._record_dry_run(opportunity, base_trade_data)

        return await self._execute_live(
            opportunity, leg1_tx, leg2_tx, base_trade_data
        )

    # ------------------------------------------------------------------
    # DRY_RUN path
    # ------------------------------------------------------------------

    async def _record_dry_run(
        self,
        opportunity: Opportunity,
        trade_data: dict,
    ) -> bool:
        """Log both pre-simulated legs as a paper trade."""
        trade_data["dry_run"] = True

        route_label = (
            "cross-issuer"
            if opportunity.is_cross_issuer()
            else "same-issuer"
        )
        msg = (
            f"DRY-RUN [{route_label}]: {opportunity.profit_pct:.4f}% profit | "
            f"In: {opportunity.input_xrp} XRP -> "
            f"IOU {opportunity.iou_currency} ({opportunity.buy_issuer[:8]}...) "
            f"-> Out: {opportunity.output_xrp} XRP"
        )
        logger.info(msg)
        await send_alert(msg)
        await log_trade(trade_data)
        return True

    # ------------------------------------------------------------------
    # LIVE path
    # ------------------------------------------------------------------

    async def _execute_live(
        self,
        opportunity: Opportunity,
        leg1_tx: dict,
        leg2_tx: dict,
        trade_data: dict,
    ) -> bool:
        """Submit leg 1, wait validated, submit leg 2, wait validated.

        Any mid-trade failure (leg 1 validated but leg 2 didn't) hands
        control to `_recover`. All pre-leg-1 failures are state-free.
        """
        trade_data["dry_run"] = False

        # ---- leg 1: submit and wait ------------------------------------------
        leg1_result = await self._submit_and_wait(leg1_tx, leg_label="leg1")
        trade_data["leg1_hash"] = leg1_result.get("tx_hash", "unknown")
        trade_data["leg1_engine_result"] = leg1_result.get("engine_result", "unknown")
        trade_data["leg1_validated"] = leg1_result.get("validated", False)

        if not leg1_result.get("success"):
            logger.error(
                f"Leg 1 failed: {leg1_result.get('engine_result')} — "
                f"no state acquired, aborting"
            )
            trade_data["error"] = (
                f"leg1_failed: {leg1_result.get('engine_result', 'unknown')}"
            )
            await log_trade(trade_data)
            await send_alert(
                f"TRADE ABORTED (leg 1): {leg1_result.get('engine_result')} | "
                f"Profit was {opportunity.profit_pct:.4f}%"
            )
            return False

        logger.info(
            f"Leg 1 validated: hash={trade_data['leg1_hash']} — "
            f"refreshing ledger for leg 2"
        )

        # ---- re-fetch current ledger for leg 2 -------------------------------
        # Leg 1 consumed ~4 ledgers while validating; leg 2's original LLS
        # is now near-expired. Fetch a fresh current_ledger and re-sign.
        fresh_info = await self._fetch_account_info()
        if fresh_info is None:
            logger.error("Could not fetch ledger state for leg 2 — handing to recovery")
            return await self._recover(
                opportunity, leg1_result, leg2_tx, trade_data,
                reason="autofill_failed_pre_leg2",
            )
        _, fresh_ledger = fresh_info
        leg2_tx["LastLedgerSequence"] = fresh_ledger + _LEDGER_WINDOW

        # ---- leg 2: submit and wait ------------------------------------------
        leg2_result = await self._submit_and_wait(leg2_tx, leg_label="leg2")
        trade_data["leg2_hash"] = leg2_result.get("tx_hash", "unknown")
        trade_data["leg2_engine_result"] = leg2_result.get("engine_result", "unknown")
        trade_data["leg2_validated"] = leg2_result.get("validated", False)

        if not leg2_result.get("success"):
            logger.error(
                f"Leg 2 failed after leg 1 committed: "
                f"{leg2_result.get('engine_result')} — entering recovery"
            )
            return await self._recover(
                opportunity, leg1_result, leg2_tx, trade_data,
                reason=f"leg2_failed: {leg2_result.get('engine_result')}",
            )

        # ---- both legs validated --------------------------------------------
        profit_xrp = opportunity.output_xrp - opportunity.input_xrp
        self.circuit_breaker.record_trade(profit_xrp)

        logger.info(
            f"LIVE EXECUTED: {opportunity.profit_pct:.4f}% profit | "
            f"leg1={trade_data['leg1_hash']} leg2={trade_data['leg2_hash']}"
        )
        await send_alert(
            f"LIVE TRADE: {opportunity.profit_pct:.4f}% profit | "
            f"In: {opportunity.input_xrp} XRP -> Out: {opportunity.output_xrp} XRP | "
            f"leg1={trade_data['leg1_hash']} leg2={trade_data['leg2_hash']}"
        )
        await log_trade(trade_data)
        return True

    # ------------------------------------------------------------------
    # Recovery flow (Phase C): retry -> market-dump -> halt+blacklist
    # ------------------------------------------------------------------

    async def _recover(
        self,
        opportunity: Opportunity,
        leg1_result: dict,
        leg2_tx: dict,
        trade_data: dict,
        reason: str,
    ) -> bool:
        """Mid-trade recovery — never requires human intervention.

        Entered after leg 1 validated but leg 2 did not. Three escalating
        steps, each time-boxed and auto-resolving:

        1. Retry leg 2 up to LEG2_RETRY_MAX times with fresh sequence and
           LastLedgerSequence. Any success records the original P&L and
           exits cleanly.
        2. Market-dump the held IOU back to XRP, accepting up to
           RECOVERY_MAX_LOSS_PCT of the entry cost as loss. Atomic floor
           on Amount ensures the loss is bounded; if the floor can't be
           met, the tx fails and we escalate.
        3. After `_RECOVERY_DUMP_ATTEMPTS` failed dumps, halt the circuit
           breaker for MID_TRADE_HALT_HOURS and blacklist the route for
           its TTL. The held IOU remains on the trust line until the
           startup recovery guard runs on next boot.

        Always returns False — even if retry/dump succeeds, the original
        opportunity did not complete cleanly, so the scan loop should not
        treat this as a normal trade success.
        """
        leg1_hash = leg1_result.get("tx_hash", "unknown")
        iou_held = Decimal(trade_data.get("iou_amount_delivered", "0"))
        trade_data["recovery_reason"] = reason
        logger.critical(
            f"MID_TRADE: leg 1 {leg1_hash[:16]}... committed, holding "
            f"{iou_held} {opportunity.iou_currency}. Reason: {reason}. "
            f"Entering recovery."
        )
        await send_alert(
            f"MID_TRADE (informational): recovering leg 1 {leg1_hash[:16]}... | "
            f"holding {iou_held} {opportunity.iou_currency} | reason: {reason}"
        )

        # ---- Step 1: retry leg 2 ----------------------------------------
        for attempt in range(1, LEG2_RETRY_MAX + 1):
            result = await self._retry_leg2(leg2_tx, attempt)
            trade_data[f"retry{attempt}_hash"] = result.get("tx_hash")
            trade_data[f"retry{attempt}_result"] = result.get("engine_result")
            if result.get("success"):
                profit_xrp = opportunity.output_xrp - opportunity.input_xrp
                self.circuit_breaker.record_trade(profit_xrp)
                trade_data["recovery_outcome"] = f"leg2_retry_{attempt}"
                trade_data["leg2_hash"] = result.get("tx_hash")
                trade_data["leg2_engine_result"] = result.get("engine_result")
                trade_data["leg2_validated"] = True
                logger.info(
                    f"Recovery retry {attempt} succeeded — "
                    f"trade completes at {opportunity.profit_pct:.4f}%"
                )
                await log_trade(trade_data)
                await send_alert(
                    f"RECOVERED on retry {attempt}: "
                    f"{opportunity.profit_pct:.4f}% profit secured"
                )
                return False  # state clean, but original execute() path didn't succeed

        # ---- Step 2: market-dump ---------------------------------------
        if iou_held > Decimal("0"):
            dump_outcome = await self._market_dump(
                opportunity, iou_held, trade_data,
            )
            if dump_outcome:
                return False  # dump succeeded, state clean, loss bounded

        # ---- Step 3: halt + blacklist ----------------------------------
        self.circuit_breaker.halt_for(
            hours=MID_TRADE_HALT_HOURS,
            reason=f"mid_trade_recovery_exhausted: {reason}",
        )
        self.blacklist.block_route(opportunity.route_key())
        trade_data["recovery_outcome"] = "halt_and_blacklist"
        trade_data["error"] = (
            f"recovery_exhausted: {reason}; held {iou_held} "
            f"{opportunity.iou_currency} awaiting startup drain"
        )
        logger.critical(
            f"Recovery exhausted. Halted for {MID_TRADE_HALT_HOURS}h, route "
            f"{opportunity.route_key()} blacklisted. Held {iou_held} "
            f"{opportunity.iou_currency} — startup drain will clean up."
        )
        await log_trade(trade_data)
        await send_alert(
            f"CRITICAL (informational): recovery exhausted after "
            f"{LEG2_RETRY_MAX} retries + {_RECOVERY_DUMP_ATTEMPTS} dumps. "
            f"Halted {MID_TRADE_HALT_HOURS}h, route blacklisted. "
            f"Held IOU drains on next boot."
        )
        return False

    async def _retry_leg2(self, leg2_tx: dict, attempt: int) -> dict:
        """Re-submit leg 2 with fresh sequence + LLS and a clean signature."""
        logger.info(f"Leg 2 retry attempt {attempt}/{LEG2_RETRY_MAX}")
        acct = await self._fetch_account_info()
        if acct is None:
            return {
                "success": False, "tx_hash": "unknown",
                "engine_result": "autofill_failed", "validated": False,
            }
        fresh_seq, fresh_ledger = acct

        # Shallow-copy and reset everything that needs to be re-derived
        retry = dict(leg2_tx)
        retry["Sequence"] = fresh_seq
        retry["Fee"] = _STANDARD_FEE_DROPS
        retry["LastLedgerSequence"] = fresh_ledger + _LEDGER_WINDOW
        retry.pop("TxnSignature", None)
        retry.pop("SigningPubKey", None)

        return await self._submit_and_wait(retry, leg_label=f"leg2-retry-{attempt}")

    async def _market_dump(
        self,
        opportunity: Opportunity,
        iou_held: Decimal,
        trade_data: dict,
    ) -> bool:
        """Attempt up to _RECOVERY_DUMP_ATTEMPTS market-dumps. True iff one
        succeeded and the wallet is clean of the held IOU."""
        try:
            dump_template = _build_market_dump_tx(
                self.wallet.address, opportunity, iou_held,
            )
        except ValueError as e:
            logger.error(f"Could not build dump tx: {e}")
            return False

        loss_cap_xrp = opportunity.input_xrp * RECOVERY_MAX_LOSS_PCT

        for attempt in range(1, _RECOVERY_DUMP_ATTEMPTS + 1):
            logger.warning(
                f"Market-dump attempt {attempt}/{_RECOVERY_DUMP_ATTEMPTS} — "
                f"accepting up to {loss_cap_xrp:.6f} XRP loss"
            )
            acct = await self._fetch_account_info()
            if acct is None:
                trade_data[f"dump{attempt}_result"] = "autofill_failed"
                continue
            fresh_seq, fresh_ledger = acct

            tx = dict(dump_template)
            tx["Sequence"] = fresh_seq
            tx["Fee"] = _STANDARD_FEE_DROPS
            tx["LastLedgerSequence"] = fresh_ledger + _LEDGER_WINDOW

            result = await self._submit_and_wait(tx, leg_label=f"dump-{attempt}")
            trade_data[f"dump{attempt}_hash"] = result.get("tx_hash")
            trade_data[f"dump{attempt}_result"] = result.get("engine_result")

            if result.get("success"):
                self.circuit_breaker.record_trade(-loss_cap_xrp)
                trade_data["recovery_outcome"] = f"dump_succeeded_attempt_{attempt}"
                logger.warning(
                    f"Market-dump succeeded on attempt {attempt}. "
                    f"Recorded {loss_cap_xrp} XRP worst-case loss."
                )
                await log_trade(trade_data)
                await send_alert(
                    f"Dump succeeded — loss capped at "
                    f"{RECOVERY_MAX_LOSS_PCT * 100:.2f}% of entry"
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Startup recovery guard — drain any held IOU on boot
    # ------------------------------------------------------------------

    async def drain_iou(
        self,
        currency: str,
        issuer: str,
        balance: Decimal,
    ) -> bool:
        """Dump a single IOU back to XRP via tfPartialPayment.

        Called by the main-loop startup guard when trust-line balances
        are non-zero on boot. No profit guarantee, no floor — we just
        want the wallet clean so normal scanning can resume.

        Returns True iff the dump validated with tesSUCCESS.
        """
        try:
            dump_tx = _build_startup_drain_tx(
                self.wallet.address, currency, issuer, balance,
            )
        except ValueError as e:
            logger.error(f"drain_iou: build failed for {currency}/{issuer[:8]}...: {e}")
            return False

        acct = await self._fetch_account_info()
        if acct is None:
            logger.error("drain_iou: account_info fetch failed")
            return False
        seq, current_ledger = acct

        dump_tx["Sequence"] = seq
        dump_tx["Fee"] = _STANDARD_FEE_DROPS
        dump_tx["LastLedgerSequence"] = current_ledger + _LEDGER_WINDOW

        logger.warning(
            f"Startup drain: dumping {balance} {currency} from issuer "
            f"{issuer[:8]}... (tfPartialPayment=true, no floor)"
        )
        result = await self._submit_and_wait(dump_tx, leg_label="startup-drain")

        if result.get("success"):
            logger.info(
                f"Startup drain OK for {currency}/{issuer[:8]}...: "
                f"hash={result.get('tx_hash')}"
            )
            return True

        logger.error(
            f"Startup drain FAILED for {currency}/{issuer[:8]}...: "
            f"{result.get('engine_result')}"
        )
        return False

    # ------------------------------------------------------------------
    # Autofill / simulate / submit / wait primitives
    # ------------------------------------------------------------------

    async def _fetch_account_info(self) -> Optional[tuple[int, int]]:
        """Return (sequence, current_ledger) via WS if open, else HTTP.

        None on failure — the caller skips the opportunity.
        """
        try:
            if self.connection and self.connection.connected:
                resp = await self.connection.send_raw({
                    "command": "account_info",
                    "account": self.wallet.address,
                    "ledger_index": "current",
                })
                result = resp.get("result", resp) if resp else {}
            else:
                payload = {
                    "method": "account_info",
                    "params": [{
                        "account": self.wallet.address,
                        "ledger_index": "current",
                    }],
                }
                resp = await asyncio.to_thread(self.rpc_client.request, payload)
                result = resp.get("result", {})
        except Exception as e:
            logger.error(f"account_info fetch raised: {e}")
            return None

        if "account_data" not in result:
            err = result.get("error_message", str(result))
            logger.error(f"account_info failed: {err}")
            return None

        sequence = result["account_data"]["Sequence"]
        current_ledger = result.get("ledger_current_index", 0)
        if not current_ledger:
            logger.error("account_info returned no ledger_current_index")
            return None
        return sequence, current_ledger

    async def _simulate(self, tx_dict: dict) -> SimResult:
        """Dispatch simulate via WS if connected, else HTTP."""
        if self.connection and self.connection.connected:
            return await simulate_transaction_ws(tx_dict, self.connection)
        return await simulate_transaction(tx_dict, self.rpc_client)

    def _sign_tx(self, tx_dict: dict) -> str:
        """Sign tx_dict in place and return the binary tx_blob.

        Wallet seed never leaves this process (T-01-10).
        """
        tx_dict["SigningPubKey"] = self.wallet.public_key
        encoded_for_signing = encode_for_signing(tx_dict)
        signature = keypairs_sign(
            bytes.fromhex(encoded_for_signing), self.wallet.private_key
        )
        tx_dict["TxnSignature"] = signature
        return xrpl_encode(tx_dict)

    async def _submit_and_wait(self, tx_dict: dict, leg_label: str) -> dict:
        """Sign, submit, and wait for validation.

        Returns a dict with keys:
            success (bool) — True iff tesSUCCESS on a validated ledger
            tx_hash (str)  — the signed tx hash
            engine_result (str) — final engine result code
            validated (bool) — whether rippled confirmed ledger inclusion
        """
        try:
            tx_blob = self._sign_tx(tx_dict)
        except Exception as e:
            logger.error(f"{leg_label} sign failed: {e}")
            return {"success": False, "tx_hash": "unknown", "engine_result": "sign_error", "validated": False}

        try:
            submit_result = await self._submit_blob(tx_blob)
        except Exception as e:
            logger.error(f"{leg_label} submit raised: {e}")
            return {"success": False, "tx_hash": "unknown", "engine_result": "submit_error", "validated": False}

        engine_result = submit_result.get("engine_result", "unknown")
        tx_hash = submit_result.get("tx_json", {}).get("hash", "unknown")

        # Terminal local failures (malformed / not-feasible) — no point polling
        if engine_result.startswith(("tem", "tef", "tel")):
            logger.error(f"{leg_label} terminally rejected at submit: {engine_result}")
            return {
                "success": False,
                "tx_hash": tx_hash,
                "engine_result": engine_result,
                "validated": False,
            }

        # Provisional result — poll tx until validated or LLS passes
        last_ledger = tx_dict.get("LastLedgerSequence", 0)
        final = await self._wait_for_validation(tx_hash, last_ledger)

        return {
            "success": final.get("engine_result") == "tesSUCCESS" and final.get("validated"),
            "tx_hash": tx_hash,
            "engine_result": final.get("engine_result", engine_result),
            "validated": final.get("validated", False),
        }

    async def _submit_blob(self, tx_blob: str) -> dict:
        """POST a signed tx_blob via submit, WS preferred."""
        if self.connection and self.connection.connected:
            resp = await self.connection.send_raw({
                "command": "submit",
                "tx_blob": tx_blob,
            })
            return resp.get("result", resp) if resp else {}

        payload = {"method": "submit", "params": [{"tx_blob": tx_blob}]}
        resp = await asyncio.to_thread(self.rpc_client.request, payload)
        return resp.get("result", {})

    async def _wait_for_validation(self, tx_hash: str, last_ledger: int) -> dict:
        """Poll `tx` until validated=true or the ledger advances past LLS.

        Returns {"engine_result": str, "validated": bool}.
        """
        while True:
            try:
                tx_result = await self._tx_lookup(tx_hash)
            except Exception as e:
                logger.warning(f"tx lookup raised while waiting: {e}")
                tx_result = {}

            validated = bool(tx_result.get("validated"))
            if validated:
                meta = tx_result.get("meta") or {}
                engine_result = meta.get("TransactionResult", "unknown")
                return {"engine_result": engine_result, "validated": True}

            # Not validated yet — check if we've run out of ledger window
            current_ledger = await self._current_ledger_safe()
            if current_ledger and current_ledger > last_ledger + _LEDGER_POLL_GRACE:
                logger.warning(
                    f"Tx {tx_hash[:16]}... never validated before ledger "
                    f"{current_ledger} (LLS was {last_ledger})"
                )
                return {"engine_result": "tefMAX_LEDGER", "validated": False}

            await asyncio.sleep(_TX_POLL_INTERVAL)

    async def _tx_lookup(self, tx_hash: str) -> dict:
        """Fetch tx record (validated flag + meta). WS preferred."""
        if self.connection and self.connection.connected:
            resp = await self.connection.send_raw({
                "command": "tx",
                "transaction": tx_hash,
                "binary": False,
            })
            return resp.get("result", resp) if resp else {}

        payload = {
            "method": "tx",
            "params": [{"transaction": tx_hash, "binary": False}],
        }
        resp = await asyncio.to_thread(self.rpc_client.request, payload)
        return resp.get("result", {})

    async def _current_ledger_safe(self) -> Optional[int]:
        """Best-effort fetch of ledger_current_index. None on error."""
        try:
            if self.connection and self.connection.connected:
                resp = await self.connection.send_raw({"command": "ledger_current"})
                result = resp.get("result", resp) if resp else {}
            else:
                payload = {"method": "ledger_current", "params": [{}]}
                resp = await asyncio.to_thread(self.rpc_client.request, payload)
                result = resp.get("result", {})
            return result.get("ledger_current_index")
        except Exception as e:
            logger.debug(f"ledger_current lookup failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Trade metadata helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_trade_metadata(
        opportunity: Opportunity,
        iou_delivered: Decimal,
        leg1_sim: SimResult,
        leg2_sim: SimResult,
    ) -> dict:
        """Build the shared trade log payload filled in by paper or live paths."""
        return {
            "profit_pct": str(opportunity.profit_pct),
            "profit_ratio": str(opportunity.profit_ratio),
            "input_xrp": str(opportunity.input_xrp),
            "output_xrp": str(opportunity.output_xrp),
            "simulated_output": str(opportunity.output_xrp),
            "iou_currency": opportunity.iou_currency,
            "buy_issuer": opportunity.buy_issuer,
            "sell_issuer": opportunity.sell_issuer or opportunity.buy_issuer,
            "iou_amount_theoretical": str(opportunity.iou_amount),
            "iou_amount_delivered": str(iou_delivered),
            "route_key": opportunity.route_key(),
            "is_cross_issuer": opportunity.is_cross_issuer(),
            "leg1_sim_result": leg1_sim.result_code,
            "leg2_sim_result": leg2_sim.result_code,
        }
