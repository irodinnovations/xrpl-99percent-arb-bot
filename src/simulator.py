"""Simulate RPC gate — no trade proceeds without tesSUCCESS.

Uses direct HTTP POST to the JSON-RPC endpoint for the simulate command,
bypassing xrpl-py model validation (which rejects cross-currency tx dicts
before they reach the network). The simulate RPC accepts raw tx_json dicts.

T-01-08: Only exact string "tesSUCCESS" in meta.TransactionResult is accepted.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import requests as http_requests

from src.config import XRPL_RPC_URL

logger = logging.getLogger(__name__)


@dataclass
class SimResult:
    """Result of a simulate RPC call."""
    success: bool
    result_code: str
    raw: Optional[dict] = None
    error: Optional[str] = None


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
        tx_result = result.get("meta", {}).get("TransactionResult", "unknown")

        if tx_result == "tesSUCCESS":
            logger.info("Simulation passed: tesSUCCESS")
            return SimResult(success=True, result_code=tx_result, raw=result)
        else:
            logger.warning(f"Simulation failed: {tx_result}")
            return SimResult(success=False, result_code=tx_result, raw=result)

    except Exception as e:
        logger.error(f"Simulate RPC error: {e}")
        return SimResult(success=False, result_code="exception", error=str(e))
