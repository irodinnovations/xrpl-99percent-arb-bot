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
