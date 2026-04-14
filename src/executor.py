"""Trade executor — DRY_RUN branching, live execution, post-trade validation.

LIVE-01: Simulation gate must pass (tesSUCCESS) before any trade proceeds.
LIVE-02: DRY_RUN=True logs without submitting any transaction.
LIVE-03: Failed live submissions logged with full error details.
DRY-01: Paper trades use real simulate RPC on mainnet data.
DRY-02: Paper trades logged identically to live trades (dry_run: true flag).

Why raw dicts for tx construction:
  xrpl-py's Payment model rejects same-account XRP-to-XRP with paths and
  send_max as a validation error before any network call. The XRPL network
  itself does allow cross-currency payment paths that begin and end in XRP
  (routed through IOU hops). We build tx_dict directly to stay consistent
  with what the pathfinder returns and what the simulate RPC actually accepts.
"""

import asyncio
import logging
from decimal import Decimal
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
# tfPartialPayment flag — required for cross-currency XRP-to-XRP loop via paths
TF_PARTIAL_PAYMENT = 131072


def _build_tx_dict(wallet_address: str, opportunity: Opportunity) -> dict:
    """Build a raw Payment transaction dict for cross-currency XRP arbitrage.

    Uses a raw dict instead of xrpl-py's Payment model because:
    - xrpl-py validates that XRP-to-XRP payments can't use paths
    - The XRPL network allows cross-currency path routing (XRP->IOU->XRP)
    - The simulate RPC and live submission both accept raw tx_json dicts

    The tfPartialPayment flag (131072) is required when both amount and
    send_max are XRP on a path payment.
    """
    input_drops = str(int(opportunity.input_xrp * DROPS_PER_XRP))
    output_drops = str(int(opportunity.output_xrp * DROPS_PER_XRP))
    # 1% send_max buffer: we spend at most 1% more than expected input
    send_max_drops = str(int(opportunity.input_xrp * DROPS_PER_XRP * Decimal("1.01")))

    return {
        "TransactionType": "Payment",
        "Account": wallet_address,
        "Amount": output_drops,
        "Destination": wallet_address,
        "Paths": opportunity.paths,
        "SendMax": send_max_drops,
        "Flags": TF_PARTIAL_PAYMENT,
    }


class TradeExecutor:
    """Executes or paper-trades opportunities after simulation gate.

    Every execution path:
    1. Circuit breaker check (halt if daily loss limit hit)
    2. Blacklist check (skip known-bad routes)
    3. Build Payment transaction dict
    4. Simulate RPC gate (reject unless tesSUCCESS — T-01-08)
    5a. DRY_RUN: log + alert, no submission
    5b. LIVE: autofill + sign + submit via JSON-RPC, record result
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
        self.connection = connection  # For WS-based simulate
        self.dry_run = dry_run

    async def execute(self, opportunity: Opportunity) -> bool:
        """Process an opportunity through simulation gate and execute/log.

        Returns True if trade was executed (paper or live), False if skipped.
        """
        # Safety checks first
        if self.circuit_breaker.is_halted():
            logger.warning("Circuit breaker HALTED — skipping trade")
            return False

        if self.blacklist.is_blacklisted(opportunity.paths):
            logger.warning("Path is blacklisted — skipping trade")
            return False

        # Build raw transaction dict (bypasses xrpl-py model validation)
        tx_dict = _build_tx_dict(self.wallet.address, opportunity)

        # SIMULATION GATE — must pass before any execution (LIVE-01, T-01-08)
        # Prefer WebSocket simulate (lower latency) with HTTP fallback
        if self.connection and self.connection.connected:
            sim_result = await simulate_transaction_ws(tx_dict, self.connection)
        else:
            sim_result = await simulate_transaction(tx_dict, self.rpc_client)
        if not sim_result.success:
            logger.warning(
                f"Simulation FAILED ({sim_result.result_code}) — trade rejected"
            )
            return False

        # Build trade data for logging (T-01-09: all trades logged)
        trade_data = {
            "profit_pct": str(opportunity.profit_pct),
            "profit_ratio": str(opportunity.profit_ratio),
            "input_xrp": str(opportunity.input_xrp),
            "output_xrp": str(opportunity.output_xrp),
            "simulated_output": str(opportunity.output_xrp),
            "dry_run": self.dry_run,
            "simulation_result": sim_result.result_code,
        }

        if self.dry_run:
            # DRY_RUN: log and alert, no submission (LIVE-02, DRY-01, DRY-02)
            msg = (
                f"DRY-RUN: {opportunity.profit_pct:.4f}% profit opportunity | "
                f"In: {opportunity.input_xrp} XRP -> Out: {opportunity.output_xrp} XRP"
            )
            logger.info(msg)
            await send_alert(msg)
            await log_trade(trade_data)
            return True

        # LIVE EXECUTION — client-side autofill, sign, submit
        # Wallet seed never leaves this process (T-01-10).
        try:
            # Step 1: Autofill — fetch Sequence and current ledger from node
            # Prefer WebSocket (already open) over HTTP for lower latency
            if self.connection and self.connection.connected:
                account_info_response = await self.connection.send_raw({
                    "command": "account_info",
                    "account": self.wallet.address,
                    "ledger_index": "current",
                })
                acct_result = account_info_response.get("result", account_info_response) if account_info_response else {}
            else:
                account_info_payload = {
                    "method": "account_info",
                    "params": [{"account": self.wallet.address, "ledger_index": "current"}],
                }
                account_info_response = await asyncio.to_thread(
                    self.rpc_client.request, account_info_payload
                )
                acct_result = account_info_response.get("result", {})

            if "account_data" not in acct_result:
                err = acct_result.get("error_message", str(acct_result))
                logger.error(f"Autofill account_info failed: {err}")
                trade_data["error"] = err
                await log_trade(trade_data)
                return False

            sequence = acct_result["account_data"]["Sequence"]
            current_ledger = acct_result.get("ledger_current_index", 0)

            tx_dict["Sequence"] = sequence
            tx_dict["Fee"] = "12"  # 12 drops, standard XRPL fee
            tx_dict["LastLedgerSequence"] = current_ledger + 4  # ~20s window
            tx_dict["SigningPubKey"] = self.wallet.public_key

            # Step 2: Client-side sign — seed never sent over the network
            encoded_for_signing = encode_for_signing(tx_dict)
            signature = keypairs_sign(
                bytes.fromhex(encoded_for_signing), self.wallet.private_key
            )
            tx_dict["TxnSignature"] = signature
            tx_blob = xrpl_encode(tx_dict)

            # Step 3: Submit — prefer WebSocket for lower latency
            if self.connection and self.connection.connected:
                submit_response = await self.connection.send_raw({
                    "command": "submit",
                    "tx_blob": tx_blob,
                })
                submit_result = submit_response.get("result", submit_response) if submit_response else {}
            else:
                submit_payload = {
                    "method": "submit",
                    "params": [{"tx_blob": tx_blob}],
                }
                submit_response = await asyncio.to_thread(
                    self.rpc_client.request, submit_payload
                )
                submit_result = submit_response.get("result", {})
            engine_result = submit_result.get("engine_result", "unknown")
            tx_hash = submit_result.get("tx_json", {}).get("hash", "unknown")

            trade_data["hash"] = tx_hash
            trade_data["engine_result"] = engine_result

            if engine_result == "tesSUCCESS":
                logger.info(
                    f"LIVE EXECUTED: {opportunity.profit_pct:.4f}% profit | Hash: {tx_hash}"
                )
                await send_alert(
                    f"LIVE TRADE: {opportunity.profit_pct:.4f}% profit | "
                    f"In: {opportunity.input_xrp} XRP -> Out: {opportunity.output_xrp} XRP | "
                    f"Hash: {tx_hash}"
                )
                # Record for circuit breaker daily P&L tracking
                profit_xrp = opportunity.output_xrp - opportunity.input_xrp
                self.circuit_breaker.record_trade(profit_xrp)
            else:
                # Failed submission — log full error details (LIVE-03)
                logger.error(
                    f"Live submission FAILED: {engine_result} | "
                    f"Full result: {submit_result}"
                )
                trade_data["error"] = str(submit_result)
                await send_alert(
                    f"TRADE FAILED: {engine_result} | "
                    f"Profit was {opportunity.profit_pct:.4f}%"
                )

            await log_trade(trade_data)
            return engine_result == "tesSUCCESS"

        except Exception as e:
            logger.error(f"Live execution error: {e}")
            trade_data["error"] = str(e)
            await log_trade(trade_data)
            await send_alert(f"EXECUTION ERROR: {e}")
            return False
