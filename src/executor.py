"""Trade executor — atomic two-leg submit (Phase 5).

Legs:
  Leg 1 converts XRP -> intermediate IOU at account Sequence N.
  Leg 2 converts intermediate IOU -> XRP at Sequence N+1.

Both legs are BUILT, SIMULATED, and SIGNED before either hits the network.
They are submitted back-to-back via submit() (submit-only, not submit_and_wait)
so leg 2's submit returns within a single WebSocket round-trip of leg 1.

If leg 1 fails terminally (tec/tef/tem), a no-op AccountSet is submitted at
Sequence N+1 to burn the pre-signed leg-2 blob. If leg 2 fails after leg 1
commits, the existing CircuitBreaker.record_trade(negative_profit) path
activates the 2% market-dump recovery logic from src/safety.py.

Replaces the prior single-Payment-loop path. No feature flag, no fallback —
the old code path is fully removed per ATOM-10.
"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from xrpl.core.binarycodec import encode as xrpl_encode, encode_for_signing
from xrpl.core.keypairs import sign as keypairs_sign
from xrpl.wallet import Wallet

from src.config import XRPL_RPC_URL, DRY_RUN, LEG2_TIMEOUT_LEDGERS
from src.pathfinder import Opportunity
from src.simulator import (
    simulate_transaction,
    simulate_transaction_ws,
    SimResult,
    HttpRpcClient,
    is_acceptable_sim_result,
)
from src.safety import CircuitBreaker, Blacklist
from src.trade_logger import log_trade, log_trade_leg, log_trade_summary
from src.telegram_alerts import send_alert

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")
TF_PARTIAL_PAYMENT = 131072  # tfPartialPayment — required on XRP path payments
BURN_FEE_DROPS = "12"
NETWORK_FEE_DROPS = "12"
LEG2_INTERMEDIATE_BUFFER = Decimal("1.005")  # 0.5% buffer on leg-2 SendMax


def _extract_intermediate(opp: Opportunity) -> tuple[str, str]:
    """Return (currency_code, issuer_address) of the IOU used to hop.

    Inspects opportunity.paths for the first non-XRP step and returns its
    currency + issuer. Raises ValueError if no clear intermediate exists.
    """
    for path in opp.paths or []:
        if not isinstance(path, list):
            continue
        for step in path:
            if not isinstance(step, dict):
                continue
            currency = step.get("currency")
            issuer = step.get("issuer")
            if currency and currency != "XRP" and issuer:
                return currency, issuer
    raise ValueError(
        f"Opportunity has no clear intermediate IOU: paths={opp.paths!r}"
    )


class TradeExecutor:
    """Executes opportunities via atomic two-leg submit (Phase 5).

    Public contract (stable): `await executor.execute(opportunity) -> bool`.
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
        # Single-writer lock — asserts only one atomic execute at a time.
        self._submit_lock = asyncio.Lock()

    async def execute(self, opportunity: Opportunity) -> bool:
        """Run an opportunity through the atomic two-leg pipeline.

        Returns True iff leg 1 AND leg 2 both reached at least the provisional
        tesSUCCESS / terPRE_SEQ acceptance. A leg-1-burn or leg-2-recovery path
        returns False.
        """
        # --- Gate 1: circuit breaker + blacklist (unchanged) ---
        if self.circuit_breaker.is_halted():
            logger.warning("Circuit breaker HALTED — skipping trade")
            return False
        if self.blacklist.is_blacklisted(opportunity.paths):
            logger.warning("Path is blacklisted — skipping trade")
            return False

        # --- Gate 2: account_info ONCE for both legs (Pitfall 2 mitigation) ---
        acct = await self._fetch_account_state()
        if acct is None:
            logger.error("Could not fetch account_info — aborting")
            return False
        sequence_n, ledger_current_index = acct
        last_ledger = ledger_current_index + LEG2_TIMEOUT_LEDGERS

        # --- Gate 3: extract intermediate IOU ---
        try:
            intermediate_currency, intermediate_issuer = _extract_intermediate(opportunity)
        except ValueError as e:
            logger.warning(f"Cannot split opportunity into legs: {e}")
            await log_trade_summary(
                outcome="pre_submit_gate_failed", dry_run=self.dry_run,
                error=f"no_intermediate: {e}",
            )
            return False

        # --- Gate 4: build both legs ---
        leg1 = self._build_leg1_tx(
            opportunity=opportunity,
            sequence=sequence_n,
            last_ledger_sequence=last_ledger,
            intermediate_currency=intermediate_currency,
            intermediate_issuer=intermediate_issuer,
        )
        # Leg 1 sim runs BEFORE leg 2 build — we need sim1's delivered_amount
        sim1 = await self._simulate(leg1)
        if not is_acceptable_sim_result(sim1.result_code, is_leg_2=False):
            logger.warning(f"Leg 1 sim rejected: {sim1.result_code}")
            await log_trade_summary(
                outcome="pre_submit_gate_failed", dry_run=self.dry_run,
                error=f"leg1_sim: {sim1.result_code}",
            )
            return False

        intermediate_amount = self._extract_sim_delivered(sim1, leg1)
        leg2 = self._build_leg2_tx(
            opportunity=opportunity,
            sequence=sequence_n + 1,
            last_ledger_sequence=last_ledger,
            intermediate_currency=intermediate_currency,
            intermediate_issuer=intermediate_issuer,
            intermediate_amount=intermediate_amount,
        )
        sim2 = await self._simulate(leg2)
        if not is_acceptable_sim_result(sim2.result_code, is_leg_2=True):
            logger.warning(f"Leg 2 sim rejected: {sim2.result_code}")
            await log_trade_summary(
                outcome="pre_submit_gate_failed", dry_run=self.dry_run,
                error=f"leg2_sim: {sim2.result_code}",
            )
            return False

        # --- DRY_RUN path: log would-execute, no submit ---
        if self.dry_run:
            msg = (
                f"DRY-RUN (atomic): {opportunity.profit_pct:.4f}% | "
                f"In: {opportunity.input_xrp} XRP -> Out: {opportunity.output_xrp} XRP | "
                f"Intermediate: {intermediate_currency}"
            )
            logger.info(msg)
            await send_alert(msg)
            await log_trade_summary(
                outcome="dry_run_would_execute",
                dry_run=True,
                profit_pct=opportunity.profit_pct,
            )
            return True

        # --- LIVE path: single-writer guard, sign, submit leg 1, submit leg 2 ---
        async with self._submit_lock:
            # Single-writer guard (ATOM-06) — re-read Sequence immediately before submit
            acct2 = await self._fetch_account_state()
            if acct2 is None or acct2[0] != sequence_n:
                actual = acct2[0] if acct2 else None
                logger.error(
                    f"Single-writer violation: Sequence drift "
                    f"(expected {sequence_n}, got {actual})"
                )
                await log_trade_summary(
                    outcome="single_writer_violation",
                    dry_run=False,
                    error=f"sequence_drift: expected={sequence_n} actual={actual}",
                )
                return False

            # Sign both legs locally (T-01-10: seed never sent over network)
            leg1_blob = self._sign_leg(leg1)
            leg2_blob = self._sign_leg(leg2)

            # Submit leg 1
            t_leg1 = time.monotonic()
            leg1_result = await self._submit_blob(leg1_blob)
            leg1_engine = leg1_result.get("engine_result", "unknown")
            leg1_hash = leg1_result.get("tx_json", {}).get("hash", "unknown")
            await log_trade_leg(
                leg=1, sequence=sequence_n, hash=leg1_hash,
                engine_result=leg1_engine, ledger_index=ledger_current_index,
                dry_run=False, latency_from_leg1_ms=None,
                path_used=leg1.get("Paths"),
            )

            # Leg 1 terminal failure (tec/tef/tem) -> burn Sequence N+1
            if _is_terminal_failure(leg1_engine):
                burn_hash, burn_ok = await self._burn_sequence(
                    sequence_n + 1, last_ledger,
                )
                outcome = "leg1_fail_burned" if burn_ok else "leg1_fail_burn_failed"
                await log_trade_summary(
                    outcome=outcome, dry_run=False,
                    leg1_hash=leg1_hash,
                    error=f"leg1_engine={leg1_engine}; burn_hash={burn_hash}",
                )
                await send_alert(
                    f"LEG 1 FAILED ({leg1_engine}) — Sequence N+1 burn: "
                    f"{'OK' if burn_ok else 'FAILED'}"
                )
                return False

            # Submit leg 2 immediately (no tx-validation wait between legs — ATOM-03)
            leg2_result = await self._submit_blob(leg2_blob)
            latency_ms = int((time.monotonic() - t_leg1) * 1000)
            leg2_engine = leg2_result.get("engine_result", "unknown")
            leg2_hash = leg2_result.get("tx_json", {}).get("hash", "unknown")
            await log_trade_leg(
                leg=2, sequence=sequence_n + 1, hash=leg2_hash,
                engine_result=leg2_engine, ledger_index=ledger_current_index,
                dry_run=False, latency_from_leg1_ms=latency_ms,
                path_used=leg2.get("Paths"),
            )

            # Leg 2 failed after leg 1 committed -> preserve 2% recovery (ATOM-05)
            if leg2_engine != "tesSUCCESS":
                est_loss = -(opportunity.input_xrp * Decimal("0.025"))
                self.circuit_breaker.record_trade(est_loss)
                await log_trade_summary(
                    outcome="leg2_fail_recovery_activated",
                    dry_run=False,
                    profit_pct=opportunity.profit_pct,
                    net_profit_xrp=est_loss,
                    leg1_hash=leg1_hash, leg2_hash=leg2_hash,
                    error=f"leg2_engine={leg2_engine}",
                )
                await send_alert(
                    f"LEG 2 FAILED ({leg2_engine}) after leg 1 committed — "
                    f"2% recovery engaged. leg1={leg1_hash} leg2={leg2_hash}"
                )
                return False

            # Both legs success
            net_profit = opportunity.output_xrp - opportunity.input_xrp
            self.circuit_breaker.record_trade(net_profit)
            await log_trade_summary(
                outcome="both_legs_success", dry_run=False,
                profit_pct=opportunity.profit_pct,
                net_profit_xrp=net_profit,
                leg1_hash=leg1_hash, leg2_hash=leg2_hash,
            )
            await send_alert(
                f"ATOMIC TRADE OK: {opportunity.profit_pct:.4f}% | "
                f"leg1={leg1_hash} leg2={leg2_hash} | latency={latency_ms}ms"
            )
            return True

    # --- Helpers ------------------------------------------------------------

    async def _fetch_account_state(self) -> Optional[tuple[int, int]]:
        """Return (Sequence, ledger_current_index) via WS if connected else HTTP."""
        if self.connection and self.connection.connected:
            resp = await self.connection.send_raw({
                "command": "account_info",
                "account": self.wallet.address,
                "ledger_index": "current",
            })
            result = (resp or {}).get("result", resp) or {}
        else:
            payload = {
                "method": "account_info",
                "params": [{"account": self.wallet.address,
                            "ledger_index": "current"}],
            }
            resp = await asyncio.to_thread(self.rpc_client.request, payload)
            result = (resp or {}).get("result", {})
        if "account_data" not in result:
            return None
        seq = int(result["account_data"]["Sequence"])
        ledger = int(result.get("ledger_current_index", 0))
        return seq, ledger

    def _build_leg1_tx(self, *, opportunity, sequence, last_ledger_sequence,
                       intermediate_currency, intermediate_issuer) -> dict:
        """Leg 1: XRP -> intermediate IOU. SendMax in drops, Amount as IOU.

        Note: same `opportunity.paths` is used for BOTH legs in v1 — per
        plan-checker Warning 5, per-leg path splitting is deferred to a
        future phase. The 100-200ms atomic window is the empirical safety
        margin against leg-1 liquidity consumption affecting leg 2.
        `log_trade_leg(path_used=...)` captures the Paths field on every
        submitted leg for post-incident diagnosis.

        Amount.value is an UPPER BOUND: tfPartialPayment delivers
        min(Amount, path-capacity). For leg 1 we don't know the exact
        delivered IOU value in advance (pathfinder's quote is a different
        ledger snapshot), so we use `input_xrp` as a GENEROUS numeric
        ceiling and let the path determine the real delivered amount.
        The real delivered IOU is read from sim1.meta.delivered_amount
        in `_extract_sim_delivered` and then used to size leg-2 SendMax.
        If this placeholder is ever misread as an exact delivery target,
        audit the tx hash + sim meta to find the true delivered value.
        The `test_atomic_all_amounts_are_decimal` test verifies the
        Decimal invariant across every field in both tx dicts.
        """
        send_max_drops = str(int(
            opportunity.input_xrp * DROPS_PER_XRP * Decimal("1.01")
        ))
        estimated_iou_value = str(opportunity.input_xrp)
        return {
            "TransactionType": "Payment",
            "Account": self.wallet.address,
            "Amount": {
                "currency": intermediate_currency,
                "issuer": intermediate_issuer,
                "value": estimated_iou_value,
            },
            "Destination": self.wallet.address,
            "Paths": opportunity.paths,
            "SendMax": send_max_drops,
            "Flags": TF_PARTIAL_PAYMENT,
            "Sequence": int(sequence),
            "Fee": NETWORK_FEE_DROPS,
            "LastLedgerSequence": int(last_ledger_sequence),
            "SigningPubKey": self.wallet.public_key,
        }

    def _build_leg2_tx(self, *, opportunity, sequence, last_ledger_sequence,
                       intermediate_currency, intermediate_issuer,
                       intermediate_amount: Decimal) -> dict:
        """Leg 2: intermediate IOU -> XRP. SendMax as IOU, Amount in drops.

        Note: same `opportunity.paths` is used for BOTH legs in v1 —
        see `_build_leg1_tx` docstring for the Warning-5 rationale.
        """
        send_max_iou = str(intermediate_amount * LEG2_INTERMEDIATE_BUFFER)
        target_xrp_drops = str(int(opportunity.output_xrp * DROPS_PER_XRP))
        return {
            "TransactionType": "Payment",
            "Account": self.wallet.address,
            "Amount": target_xrp_drops,
            "Destination": self.wallet.address,
            "Paths": opportunity.paths,
            "SendMax": {
                "currency": intermediate_currency,
                "issuer": intermediate_issuer,
                "value": send_max_iou,
            },
            "Flags": TF_PARTIAL_PAYMENT,
            "Sequence": int(sequence),
            "Fee": NETWORK_FEE_DROPS,
            "LastLedgerSequence": int(last_ledger_sequence),
            "SigningPubKey": self.wallet.public_key,
        }

    @staticmethod
    def _extract_sim_delivered(sim: SimResult, tx_dict: dict) -> Decimal:
        """Return delivered IOU amount from sim meta; fall back to tx Amount."""
        raw = sim.raw or {}
        meta = raw.get("meta") or {}
        delivered = meta.get("delivered_amount")
        if isinstance(delivered, dict):
            return Decimal(str(delivered.get("value", "0")))
        if isinstance(delivered, str):
            return Decimal(delivered) / DROPS_PER_XRP
        amt = tx_dict.get("Amount")
        if isinstance(amt, dict):
            return Decimal(str(amt.get("value", "0")))
        return Decimal("0")

    async def _simulate(self, tx_dict: dict) -> SimResult:
        if self.connection and self.connection.connected:
            return await simulate_transaction_ws(tx_dict, self.connection)
        return await simulate_transaction(tx_dict, self.rpc_client)

    def _sign_leg(self, tx_dict: dict) -> str:
        encoded = encode_for_signing(tx_dict)
        sig = keypairs_sign(bytes.fromhex(encoded), self.wallet.private_key)
        tx_dict["TxnSignature"] = sig
        return xrpl_encode(tx_dict)

    async def _submit_blob(self, tx_blob: str) -> dict:
        if self.connection and self.connection.connected:
            resp = await self.connection.send_raw({
                "command": "submit", "tx_blob": tx_blob,
            })
            return (resp or {}).get("result", resp) or {}
        payload = {"method": "submit", "params": [{"tx_blob": tx_blob}]}
        resp = await asyncio.to_thread(self.rpc_client.request, payload)
        return (resp or {}).get("result", {})

    async def _burn_sequence(self, sequence_to_burn: int,
                              last_ledger: int) -> tuple[str, bool]:
        """No-op AccountSet at given Sequence to prevent leg-2 replay (ATOM-04).

        Returns (hash, success). Hand-rolls a raw AccountSet tx dict through
        the same `_sign_leg` + `_submit_blob` path used for Payment legs.
        We intentionally do NOT import `xrpl.models.transactions.AccountSet`
        or `xrpl.asyncio.transaction.autofill_and_sign` — the hand-rolled
        path has fewer moving parts and mirrors the rest of this file.
        """
        burn_tx = {
            "TransactionType": "AccountSet",
            "Account": self.wallet.address,
            "Sequence": int(sequence_to_burn),
            "Fee": BURN_FEE_DROPS,
            "LastLedgerSequence": int(last_ledger),
            "SigningPubKey": self.wallet.public_key,
        }
        try:
            blob = self._sign_leg(burn_tx)
            result = await self._submit_blob(blob)
            engine = result.get("engine_result", "unknown")
            hash_ = result.get("tx_json", {}).get("hash", "unknown")
            ok = engine == "tesSUCCESS"
            logger.info(f"Sequence {sequence_to_burn} burn: {engine} hash={hash_}")
            return hash_, ok
        except Exception as e:
            logger.error(f"Burn failed: {e}")
            return "unknown", False


def _is_terminal_failure(engine_result: str) -> bool:
    """tec/tef/tem codes are terminal — sequence consumed, not retryable."""
    return engine_result.startswith(("tec", "tef", "tem"))
