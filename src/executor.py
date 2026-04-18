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
from decimal import Decimal, InvalidOperation
from typing import Optional

from xrpl.core.binarycodec import encode as xrpl_encode, encode_for_signing
from xrpl.core.keypairs import sign as keypairs_sign
from xrpl.wallet import Wallet

from src.config import XRPL_RPC_URL, DRY_RUN
from src.pathfinder import Opportunity
from src.simulator import simulate_transaction, simulate_transaction_ws, SimResult, HttpRpcClient
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


def _extract_delivered_iou(sim_raw: Optional[dict]) -> Optional[Decimal]:
    """Pull the IOU value delivered by a leg-1 simulate response.

    rippled attaches `delivered_amount` to meta on any payment that would
    apply (tesSUCCESS). For XRP->IOU it is the IOU object we just acquired.

    Returns None when the field is missing, XRP-typed, or malformed — the
    caller treats that as a simulate quality failure and skips the opp.
    """
    if not sim_raw:
        return None

    meta = sim_raw.get("meta") or {}
    delivered = meta.get("delivered_amount")

    # IOU delivery: object with currency/issuer/value
    if isinstance(delivered, dict):
        value = delivered.get("value")
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if parsed <= Decimal("0"):
            return None
        return parsed

    # XRP delivery (string of drops) — not expected on leg 1, treat as None
    return None


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

    async def execute(self, opportunity: Opportunity) -> bool:
        """Run an opportunity through the two-leg pipeline.

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

        # Legacy multi-hop opps (from ripple_path_find) have no two-leg
        # metadata. Skip them cleanly — phase B4 rewrites pathfinder.
        if not opportunity.iou_currency or not opportunity.buy_issuer:
            logger.info(
                "Skipping legacy multi-hop opportunity (no two-leg metadata "
                "— phase B4 will rewrite pathfinder to emit two-leg shape)"
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
            logger.warning(
                f"Leg 1 simulation FAILED ({leg1_sim.result_code}) — "
                f"route rejected, no state acquired"
            )
            return False

        iou_delivered = _extract_delivered_iou(leg1_sim.raw)
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
            logger.warning(
                f"Leg 2 simulation FAILED ({leg2_sim.result_code}) — "
                f"route rejected, no state acquired"
            )
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
    # Recovery (STUB — phase C implements retry / market-dump / halt)
    # ------------------------------------------------------------------

    async def _recover(
        self,
        opportunity: Opportunity,
        leg1_result: dict,
        leg2_tx: dict,
        trade_data: dict,
        reason: str,
    ) -> bool:
        """STUB: mid-trade recovery — phase C will replace this.

        In phase B2 we log the MID_TRADE state and return False so the
        scan loop can continue. The VPS bot-startup recovery guard (phase
        B5) will drain any held IOU on the next restart; in the interim
        the held IOU simply sits on the trust line.
        """
        leg1_hash = leg1_result.get("tx_hash", "unknown")
        logger.critical(
            f"MID_TRADE state: leg 1 committed ({leg1_hash}) but leg 2 did not. "
            f"Reason: {reason}. Phase B2 stub: held IOU remains on trust line. "
            f"Phase C will retry, market-dump, or halt+blacklist."
        )
        trade_data["recovery_stub"] = True
        trade_data["recovery_reason"] = reason
        trade_data["error"] = f"mid_trade_stub: {reason}"
        await log_trade(trade_data)
        await send_alert(
            f"MID_TRADE (informational): leg 1 {leg1_hash[:16]}... committed, "
            f"leg 2 failed ({reason}). IOU held on trust line. "
            f"Phase C recovery not yet implemented."
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
