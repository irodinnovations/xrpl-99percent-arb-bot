"""Simulate RPC gate — no trade proceeds without tesSUCCESS.

Uses direct HTTP POST to the JSON-RPC endpoint for the simulate command,
bypassing xrpl-py model validation (which rejects cross-currency tx dicts
before they reach the network). The simulate RPC accepts raw tx_json dicts.

T-01-08: Only exact string "tesSUCCESS" in meta.TransactionResult is accepted.

Two-leg pre-simulation
----------------------
For two-leg arbitrage the executor calls `simulate_transaction` twice per
opportunity: leg 1 (XRP->IOU) and leg 2 (IOU->XRP) with Sequence+1. Leg 2
is parameterized from leg 1's `delivered_amount`, which this module exposes
as a typed field on SimResult so callers do not need to dig into raw meta.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Protocol

import requests as http_requests

from src.config import XRPL_RPC_URL

logger = logging.getLogger(__name__)


@dataclass
class SimResult:
    """Result of a simulate RPC call.

    `delivered_amount` is auto-populated from meta.delivered_amount and is
    None when the transaction didn't apply, the field was absent, or it
    was an XRP string (leg-1 arbitrage always expects an IOU dict).
    """
    success: bool
    result_code: str
    raw: Optional[dict] = None
    error: Optional[str] = None
    delivered_amount: Optional[dict] = None  # IOU dict: {currency, issuer, value}

    def delivered_iou_value(self) -> Optional[Decimal]:
        """Return the Decimal value of an IOU delivery, or None.

        Returns None when:
          - sim did not succeed (no meta.delivered_amount),
          - delivered_amount was XRP drops (a string, not a dict),
          - the value string was malformed or zero.
        """
        return extract_delivered_iou(self.delivered_amount)


def extract_delivered_iou(delivered_amount: Any) -> Optional[Decimal]:
    """Parse an XRPL delivered_amount value into a positive Decimal.

    Accepts either the meta.delivered_amount field directly (IOU dict or
    XRP drops string) or None. Returns None for XRP deliveries, missing
    fields, malformed values, and non-positive amounts.
    """
    if not isinstance(delivered_amount, dict):
        return None
    value = delivered_amount.get("value")
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if parsed <= Decimal("0"):
        return None
    return parsed


def _pull_delivered_amount(result: dict) -> Optional[dict]:
    """Return meta.delivered_amount as a dict, or None for XRP / missing."""
    if not isinstance(result, dict):
        return None
    meta = result.get("meta") or {}
    delivered = meta.get("delivered_amount")
    if isinstance(delivered, dict):
        return delivered
    return None


class RpcClientProtocol(Protocol):
    """Protocol for the RPC client used in simulate — allows mocking in tests."""

    def request(self, payload: dict) -> dict:
        """POST payload to RPC endpoint, return parsed JSON response."""
        ...


class HttpRpcClient:
    """Thin HTTP client for XRPL JSON-RPC calls.

    Separate from xrpl-py's JsonRpcClient to avoid model validation constraints
    when building simulate payloads with cross-currency tx_json dicts.
    """

    def __init__(self, url: str = XRPL_RPC_URL):
        self.url = url

    def request(self, payload: dict) -> dict:
        """POST JSON payload to XRPL RPC endpoint. Returns parsed JSON."""
        response = http_requests.post(self.url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()


def _build_rpc_client() -> HttpRpcClient:
    """Create an HTTP RPC client for simulate calls."""
    return HttpRpcClient(XRPL_RPC_URL)


async def simulate_transaction(
    tx_dict: dict,
    rpc_client=None,
) -> SimResult:
    """Run simulate RPC against live ledger. Returns SimResult.

    Only returns success=True if TransactionResult == "tesSUCCESS".
    Any other result or exception returns success=False.

    T-01-08: Exact string match on "tesSUCCESS" — no partial matches accepted.

    Args:
        tx_dict: Raw transaction dict (Payment, OfferCreate, etc.)
        rpc_client: Injectable client for testing. Must have .request(payload) -> dict.
    """
    if rpc_client is None:
        rpc_client = _build_rpc_client()

    try:
        payload = {
            "method": "simulate",
            "params": [{"tx_json": tx_dict, "binary": False}],
        }
        raw_response = await asyncio.to_thread(rpc_client.request, payload)

        # JSON-RPC error at transport level
        if "error" in raw_response:
            error_msg = raw_response.get("error", {})
            return SimResult(
                success=False,
                result_code="rpc_error",
                error=str(error_msg),
            )

        result = raw_response.get("result", {})
        tx_result = _extract_result_code(result)

        delivered = _pull_delivered_amount(result)
        if tx_result == "tesSUCCESS":
            logger.info("Simulation passed: tesSUCCESS")
            return SimResult(
                success=True, result_code=tx_result, raw=result,
                delivered_amount=delivered,
            )
        else:
            logger.warning(f"Simulation failed: {tx_result}")
            return SimResult(
                success=False, result_code=tx_result, raw=result,
                delivered_amount=delivered,
            )

    except Exception as e:
        logger.error(f"Simulate RPC error: {e}")
        return SimResult(success=False, result_code="exception", error=str(e))


def _extract_result_code(result: dict) -> str:
    """Pull the transaction result code from a simulate RPC response.

    The simulate RPC returns `engine_result` at the top level of the result
    object for every response (success AND failure). `meta.TransactionResult`
    is only populated when the transaction would have applied — on path
    failures like tecPATH_DRY the `meta` object may be missing entirely.

    Checking engine_result first is required; meta.TransactionResult is a
    defensive fallback.
    """
    engine_result = result.get("engine_result")
    if engine_result:
        return engine_result
    meta_result = result.get("meta", {}).get("TransactionResult")
    if meta_result:
        return meta_result
    return "unknown"


async def simulate_transaction_ws(
    tx_dict: dict,
    connection,
) -> SimResult:
    """Run simulate RPC via WebSocket instead of HTTP.

    Uses the already-open WebSocket connection, eliminating the overhead of
    asyncio.to_thread + HTTP POST per simulate call.  Same tesSUCCESS gate
    as the HTTP version.

    Falls back to HTTP simulate if the WebSocket send fails.

    Args:
        tx_dict: Raw transaction dict (must be unsigned).
        connection: XRPLConnection instance with an active WebSocket.
    """
    try:
        payload = {
            "command": "simulate",
            "tx_json": tx_dict,
            "binary": False,
        }
        raw_response = await connection.send_raw(payload)

        if raw_response is None:
            logger.warning("WS simulate returned None — falling back to HTTP")
            return await simulate_transaction(tx_dict)

        # Server-level error on the WS channel. Some clio/rippled WS
        # endpoints reject or mis-route `simulate` even when HTTP-RPC
        # accepts the identical tx_json. Log the detail and fall back
        # to HTTP rather than giving up — HTTP is the authoritative
        # path for this endpoint.
        if "error" in raw_response:
            error_msg = raw_response.get("error", {})
            logger.warning(
                f"WS simulate returned rpc_error ({error_msg}) — "
                f"falling back to HTTP"
            )
            return await simulate_transaction(tx_dict)

        result = raw_response.get("result", raw_response)
        tx_result = _extract_result_code(result)

        delivered = _pull_delivered_amount(result)
        if tx_result == "tesSUCCESS":
            logger.info("Simulation passed (WS): tesSUCCESS")
            return SimResult(
                success=True, result_code=tx_result, raw=result,
                delivered_amount=delivered,
            )
        else:
            logger.warning(f"Simulation failed (WS): {tx_result}")
            return SimResult(
                success=False, result_code=tx_result, raw=result,
                delivered_amount=delivered,
            )

    except Exception as e:
        logger.warning(f"WS simulate error: {e} — falling back to HTTP")
        return await simulate_transaction(tx_dict)
