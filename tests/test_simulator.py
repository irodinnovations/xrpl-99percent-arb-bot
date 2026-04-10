"""Tests for simulate RPC gate."""

import pytest
from unittest.mock import MagicMock
from src.simulator import simulate_transaction, SimResult


@pytest.mark.asyncio
async def test_simulate_success():
    """simulate_transaction returns success=True when RPC returns tesSUCCESS."""
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
async def test_simulate_tec_path_dry():
    """simulate_transaction returns success=False when RPC returns tecPATH_DRY."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "meta": {"TransactionResult": "tecPATH_DRY"}
        }
    }

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "tecPATH_DRY"


@pytest.mark.asyncio
async def test_simulate_exception():
    """simulate_transaction returns success=False on RPC exception."""
    mock_client = MagicMock()
    mock_client.request.side_effect = ConnectionError("timeout")

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "exception"
    assert "timeout" in result.error
