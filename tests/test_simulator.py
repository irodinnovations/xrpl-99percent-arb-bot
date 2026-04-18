"""Tests for simulate RPC gate.

The simulate RPC returns `engine_result` at the top level of the result
object on every response. `meta.TransactionResult` is only present when
the transaction would have applied — path failures like tecPATH_DRY
return only `engine_result`. These tests cover both shapes.
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock
from src.simulator import simulate_transaction, SimResult, extract_delivered_iou


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


# ---------------------------------------------------------------------------
# delivered_amount extraction (used by two-leg executor to parameterize leg 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulate_populates_delivered_amount_on_success():
    """On a successful IOU-targeting payment, SimResult.delivered_amount is
    populated from meta.delivered_amount and delivered_iou_value() returns
    the Decimal value."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "engine_result": "tesSUCCESS",
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": {
                    "currency": "USD",
                    "issuer": "rIssuer111",
                    "value": "3.141592",
                },
            },
        }
    }
    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.delivered_amount == {
        "currency": "USD",
        "issuer": "rIssuer111",
        "value": "3.141592",
    }
    assert result.delivered_iou_value() == Decimal("3.141592")


@pytest.mark.asyncio
async def test_simulate_delivered_amount_none_on_failure():
    """A failed sim has no meta.delivered_amount — delivered_iou_value() is None."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {"engine_result": "tecPATH_DRY"}
    }
    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    assert result.delivered_amount is None
    assert result.delivered_iou_value() is None


@pytest.mark.asyncio
async def test_simulate_xrp_delivery_yields_no_iou_value():
    """An XRP delivery (string drops) is not an IOU — delivered_iou_value None."""
    mock_client = MagicMock()
    mock_client.request.return_value = {
        "result": {
            "engine_result": "tesSUCCESS",
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": "5000000",  # 5 XRP in drops as a string
            },
        }
    }
    result = await simulate_transaction({"Account": "rTest"}, mock_client)
    # raw is populated, but typed delivered_amount only captures IOU dicts
    assert result.delivered_amount is None
    assert result.delivered_iou_value() is None


class TestExtractDeliveredIou:
    def test_iou_dict_returns_decimal(self):
        assert extract_delivered_iou(
            {"currency": "USD", "issuer": "rX", "value": "2.5"}
        ) == Decimal("2.5")

    def test_non_dict_returns_none(self):
        assert extract_delivered_iou("5000000") is None
        assert extract_delivered_iou(None) is None

    def test_missing_value_returns_none(self):
        assert extract_delivered_iou({"currency": "USD"}) is None

    def test_malformed_value_returns_none(self):
        assert extract_delivered_iou({"value": "not-a-number"}) is None

    def test_zero_value_returns_none(self):
        assert extract_delivered_iou({"value": "0"}) is None

    def test_negative_value_returns_none(self):
        assert extract_delivered_iou({"value": "-1.5"}) is None
