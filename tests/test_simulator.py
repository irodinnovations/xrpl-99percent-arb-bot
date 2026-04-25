"""Tests for simulate RPC gate.

The simulate RPC returns `engine_result` at the top level of the result
object on every response. `meta.TransactionResult` is only present when
the transaction would have applied — path failures like tecPATH_DRY
return only `engine_result`. These tests cover both shapes.
"""

import pytest
from unittest.mock import MagicMock
from src.simulator import simulate_transaction, SimResult


@pytest.mark.asyncio
async def test_simulate_success_engine_result():
    """Success via engine_result (the real RPC shape)."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "applied": True,
            "engine_result": "tesSUCCESS",
            "engine_result_code": 0,
            "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": []},
        }
    }

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is True
    assert result.result_code == "tesSUCCESS"


@pytest.mark.asyncio
async def test_simulate_tec_path_dry_engine_result_only():
    """tecPATH_DRY returns engine_result but no meta.TransactionResult.

    This is the real shape of a failed simulate where no path could
    deliver the requested amount — the case that was silently returning
    'unknown' before the fix.
    """
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "applied": False,
            "engine_result": "tecPATH_DRY",
            "engine_result_code": 128,
            "engine_result_message": "Path could not send partial amount.",
        }
    }

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "tecPATH_DRY"


@pytest.mark.asyncio
async def test_simulate_fallback_to_meta_transaction_result():
    """Backward-compatible: older/alternative responses with only meta.TransactionResult."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "meta": {"TransactionResult": "tesSUCCESS"}
        }
    }

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is True
    assert result.result_code == "tesSUCCESS"


@pytest.mark.asyncio
async def test_simulate_unknown_when_no_result_fields():
    """If neither engine_result nor meta.TransactionResult is present, return 'unknown'."""
    mock_client = MagicMock()
    mock_client.request.return_value = {"result": {}}

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "unknown"


@pytest.mark.asyncio
async def test_simulate_exception():
    """simulate_transaction returns success=False on RPC exception."""
    mock_client = MagicMock()
    mock_client.request.side_effect = ConnectionError("timeout")

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "exception"
    assert "timeout" in result.error


# --- Leg-2 simulate acceptance helper (ATOM-07) ---

from src.simulator import is_acceptable_sim_result, LEG2_ACCEPTABLE_CODES


def test_leg2_acceptable_codes_is_frozenset_of_two_values():
    assert isinstance(LEG2_ACCEPTABLE_CODES, frozenset)
    assert LEG2_ACCEPTABLE_CODES == frozenset({"tesSUCCESS", "terPRE_SEQ"})


def test_is_acceptable_sim_result_leg1_strict():
    assert is_acceptable_sim_result("tesSUCCESS", is_leg_2=False) is True
    # Leg 1 must never treat terPRE_SEQ as pass
    assert is_acceptable_sim_result("terPRE_SEQ", is_leg_2=False) is False
    assert is_acceptable_sim_result("tecPATH_PARTIAL", is_leg_2=False) is False
    assert is_acceptable_sim_result("unknown", is_leg_2=False) is False


def test_is_acceptable_sim_result_leg2_accepts_terpre_seq():
    assert is_acceptable_sim_result("tesSUCCESS", is_leg_2=True) is True
    assert is_acceptable_sim_result("terPRE_SEQ", is_leg_2=True) is True


def test_is_acceptable_sim_result_leg2_rejects_terminal_failures():
    for code in ("tecPATH_PARTIAL", "tecPATH_DRY", "tefMAX_LEDGER",
                 "temBAD_AMOUNT", "unknown", "rpc_error", "exception"):
        assert is_acceptable_sim_result(code, is_leg_2=True) is False, (
            f"{code} must not pass leg-2 gate"
        )


def test_existing_simulate_helpers_unchanged_still_strict():
    # Regression guard: SimResult.success stays strict-tesSUCCESS even for terPRE_SEQ.
    # This protects every existing non-atomic caller from accidentally accepting terPRE_SEQ.
    import pytest
    from unittest.mock import MagicMock
    from src.simulator import simulate_transaction

    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "applied": False,
            "engine_result": "terPRE_SEQ",
            "engine_result_code": -98,
        }
    }

    async def _run():
        return await simulate_transaction({"Account": "rTest"}, mock_client)

    import asyncio
    result = asyncio.run(_run())
    # SimResult.success must stay False on terPRE_SEQ — strict gate unchanged
    assert result.success is False
    assert result.result_code == "terPRE_SEQ"
    # But the NEW helper accepts it for leg-2 use
    assert is_acceptable_sim_result(result.result_code, is_leg_2=True) is True


# --- WebSocket simulate + leg-2 helper integration (ATOM-07) ---


@pytest.mark.asyncio
async def test_simulate_ws_terpre_seq_flows_through_to_leg2_helper():
    """Leg-2 sim returns terPRE_SEQ via WS; helper accepts it; SimResult.success stays False."""
    from unittest.mock import AsyncMock, MagicMock
    from src.simulator import simulate_transaction_ws, is_acceptable_sim_result

    mock_connection = MagicMock()
    mock_connection.connected = True
    mock_connection.send_raw = AsyncMock(return_value={
        "result": {
            "applied": False,
            "engine_result": "terPRE_SEQ",
            "engine_result_code": -98,
            "engine_result_message": "Missing/inapplicable prior transaction.",
        }
    })

    result = await simulate_transaction_ws({"Account": "rTest"}, mock_connection)

    # Strict SimResult.success stays False — regression guard for leg-1 callers
    assert result.success is False
    assert result.result_code == "terPRE_SEQ"

    # Leg-2 helper accepts it
    assert is_acceptable_sim_result(result.result_code, is_leg_2=True) is True
    # Leg-1 helper rejects it
    assert is_acceptable_sim_result(result.result_code, is_leg_2=False) is False

    # Confirm ws send_raw was the only path called (no HTTP fallback)
    mock_connection.send_raw.assert_awaited_once()


@pytest.mark.asyncio
async def test_simulate_ws_tessuccess_accepted_by_both_helpers():
    """Happy path — leg 1 AND leg 2 both accept tesSUCCESS."""
    from unittest.mock import AsyncMock, MagicMock
    from src.simulator import simulate_transaction_ws, is_acceptable_sim_result

    mock_connection = MagicMock()
    mock_connection.connected = True
    mock_connection.send_raw = AsyncMock(return_value={
        "result": {
            "applied": True,
            "engine_result": "tesSUCCESS",
            "engine_result_code": 0,
        }
    })

    result = await simulate_transaction_ws({"Account": "rTest"}, mock_connection)

    assert result.success is True
    assert is_acceptable_sim_result(result.result_code, is_leg_2=True) is True
    assert is_acceptable_sim_result(result.result_code, is_leg_2=False) is True


# ============================================================================
# WS error → HTTP fallback regression guard (production rpc_error fix 2026-04-25)
# ============================================================================
#
# Bug: WS simulate returned SimResult(result_code="rpc_error") whenever the
# response had an "error" field set. This happens consistently in production
# due to xrpl-py's "Request RequestMethod.X is already in progress" race
# condition on concurrent WS requests sharing the same internal request ID.
# The executor's diagnostics (PR #21) revealed 100% of leg-1 sim failures
# were rpc_error, dropping every opportunity in the 27-hour window after
# atomic deployment.
#
# Fix: when WS returns an error response, fall back to HTTP simulate (same
# pattern already used for None responses and exceptions).


@pytest.mark.asyncio
async def test_simulate_ws_error_falls_back_to_http():
    """WS error response must fall back to HTTP, not return rpc_error.

    Previously: error in raw_response -> return SimResult(rpc_error). This
    silently dropped opportunities whenever xrpl-py's WS request manager hit
    a request-ID collision (which it does often under load).
    """
    from unittest.mock import AsyncMock, patch
    from src.simulator import simulate_transaction_ws

    # WS connection that returns an error response (collision scenario)
    mock_connection = MagicMock()
    mock_connection.send_raw = AsyncMock(return_value={
        "error": "Request RequestMethod.SIMULATE_99999 is already in progress.",
    })

    # HTTP fallback returns a successful sim — the fallback should be hit
    # and its result returned, NOT rpc_error.
    fallback_result = SimResult(
        success=True,
        result_code="tesSUCCESS",
        raw={"engine_result": "tesSUCCESS"},
    )

    with patch("src.simulator.simulate_transaction", new_callable=AsyncMock) as mock_http:
        mock_http.return_value = fallback_result
        result = await simulate_transaction_ws({"Account": "rTest"}, mock_connection)

    # Fallback was invoked exactly once
    mock_http.assert_called_once()

    # Result is the HTTP fallback's result, not rpc_error
    assert result.result_code == "tesSUCCESS"
    assert result.success is True
    # Hard regression guard: never return rpc_error on WS error path
    assert result.result_code != "rpc_error"


@pytest.mark.asyncio
async def test_simulate_ws_error_falls_back_to_http_even_when_http_also_fails():
    """If both WS and HTTP fail, the HTTP failure result is propagated.

    Demonstrates the fallback is unconditional — we always try HTTP after
    a WS error, and the caller sees whatever HTTP returns (success OR
    failure). This is correct: even an HTTP-side rpc_error is more
    informative than blindly trusting a stale WS race condition.
    """
    from unittest.mock import AsyncMock, patch
    from src.simulator import simulate_transaction_ws

    mock_connection = MagicMock()
    mock_connection.send_raw = AsyncMock(return_value={
        "error": "Request RequestMethod.SIMULATE_42 is already in progress.",
    })

    http_failure = SimResult(success=False, result_code="rpc_error",
                             error="HTTP also failed")

    with patch("src.simulator.simulate_transaction", new_callable=AsyncMock) as mock_http:
        mock_http.return_value = http_failure
        result = await simulate_transaction_ws({"Account": "rTest"}, mock_connection)

    mock_http.assert_called_once()
    # If HTTP also fails, propagating its result is correct behavior.
    assert result.success is False
