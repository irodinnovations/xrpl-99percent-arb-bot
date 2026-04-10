"""Tests for simulate RPC gate."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from src.simulator import simulate_transaction, SimResult


@pytest.mark.asyncio
async def test_simulate_success():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.is_successful.return_value = True
    mock_response.result = {
        "meta": {"TransactionResult": "tesSUCCESS"}
    }
    mock_client.request.return_value = mock_response

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is True
    assert result.result_code == "tesSUCCESS"


@pytest.mark.asyncio
async def test_simulate_tec_path_dry():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.is_successful.return_value = True
    mock_response.result = {
        "meta": {"TransactionResult": "tecPATH_DRY"}
    }
    mock_client.request.return_value = mock_response

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "tecPATH_DRY"


@pytest.mark.asyncio
async def test_simulate_exception():
    mock_client = MagicMock()
    mock_client.request.side_effect = ConnectionError("timeout")

    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.success is False
    assert result.result_code == "exception"
    assert "timeout" in result.error
