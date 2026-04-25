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


# Leg-2 simulate acceptance set (ATOM-07).
# When the atomic two-leg executor (src/executor.py) pre-simulates leg 2
# against CURRENT ledger state, leg 2's Sequence is N+1 while the account's
# Sequence is N — rippled returns `terPRE_SEQ` ("sequence ahead of account
# sequence"). That is the state-dependent "would pass once leg 1 applies"
# signal and MUST be treated as a pass for leg-2 sims only. Leg 1 always
# uses exact `tesSUCCESS`.
# See: https://xrpl.org/docs/references/protocol/transactions/transaction-results/ter-codes
LEG2_ACCEPTABLE_CODES: frozenset[str] = frozenset({"tesSUCCESS", "terPRE_SEQ"})


def is_acceptable_sim_result(result_code: str, *, is_leg_2: bool) -> bool:
    """Whitelist check for simulate result codes.

    Leg 1 must be exact `tesSUCCESS` (strict gate — an "unknown" or
    `terPRE_SEQ` on leg 1 would indicate the account Sequence was
    misconfigured and is never acceptable).

    Leg 2 accepts `tesSUCCESS` OR `terPRE_SEQ` — the latter is the
    canonical state-dependent pass when Sequence N+1 is simulated
    against account Sequence N.

    This helper is consumed by the atomic executor; the standard
    SimResult.success flag is unchanged (still strict tesSUCCESS).
    """
    if is_leg_2:
        return result_code in LEG2_ACCEPTABLE_CODES
    return result_code == "tesSUCCESS"


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

        if tx_result == "tesSUCCESS":
            logger.info("Simulation passed: tesSUCCESS")
            return SimResult(success=True, result_code=tx_result, raw=result)
        else:
            logger.warning(f"Simulation failed: {tx_result}")
            return SimResult(success=False, result_code=tx_result, raw=result)

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

        # Server-level error response. This includes xrpl-py's internal
        # "RequestMethod.X is already in progress" race condition where a
        # request-ID collision aborts the call. We previously returned
        # rpc_error here and dropped the opportunity, but the executor's
        # diagnostics (PR #21) showed 100% of leg-1 sim failures were
        # rpc_error — meaning every opportunity was lost to this path.
        # Fall back to HTTP instead — HTTP uses a fresh connection and
        # doesn't suffer from the WS request-ID collision problem.
        if "error" in raw_response:
            error_msg = raw_response.get("error", {})
            logger.warning(
                f"WS simulate returned error ({error_msg}) — "
                f"falling back to HTTP"
            )
            return await simulate_transaction(tx_dict)

        result = raw_response.get("result", raw_response)
        tx_result = _extract_result_code(result)

        if tx_result == "tesSUCCESS":
            logger.info("Simulation passed (WS): tesSUCCESS")
            return SimResult(success=True, result_code=tx_result, raw=result)
        else:
            logger.warning(f"Simulation failed (WS): {tx_result}")
            return SimResult(success=False, result_code=tx_result, raw=result)

    except Exception as e:
        logger.warning(f"WS simulate error: {e} — falling back to HTTP")
        return await simulate_transaction(tx_dict)
