"""Tests for JSONL trade logger."""

import json
import os
import pytest
import tempfile
from decimal import Decimal
from unittest.mock import patch

# Patch LOG_FILE before importing
TEST_LOG = tempfile.mktemp(suffix=".jsonl")


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test log file after each test."""
    yield
    if os.path.exists(TEST_LOG):
        os.unlink(TEST_LOG)


@pytest.fixture
def patch_log_file():
    with patch("src.trade_logger.LOG_FILE", TEST_LOG):
        yield


@pytest.mark.asyncio
async def test_log_trade_writes_json_line(patch_log_file):
    from src.trade_logger import log_trade
    await log_trade({
        "profit_pct": "0.7",
        "input_xrp": "5",
        "simulated_output": "5.035",
        "dry_run": True,
    })

    with open(TEST_LOG, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "timestamp" in entry
    assert entry["profit_pct"] == "0.7"
    assert entry["dry_run"] is True


@pytest.mark.asyncio
async def test_log_trade_appends(patch_log_file):
    from src.trade_logger import log_trade
    await log_trade({"profit_pct": "0.5", "dry_run": True})
    await log_trade({"profit_pct": "0.8", "dry_run": True})

    with open(TEST_LOG, "r") as f:
        lines = f.readlines()
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_log_entry_has_timestamp(patch_log_file):
    from src.trade_logger import log_trade
    await log_trade({"profit_pct": "0.7", "dry_run": True})

    with open(TEST_LOG, "r") as f:
        entry = json.loads(f.readline())
    assert "timestamp" in entry
    # ISO format check: contains 'T' separator
    assert "T" in entry["timestamp"]


@pytest.mark.asyncio
async def test_log_entry_preserves_all_fields(patch_log_file):
    from src.trade_logger import log_trade
    data = {
        "profit_pct": "0.7",
        "input_xrp": "5",
        "simulated_output": "5.035",
        "dry_run": True,
        "hash": "ABC123",
    }
    await log_trade(data)

    with open(TEST_LOG, "r") as f:
        entry = json.loads(f.readline())
    for key in data:
        assert key in entry, f"Missing key: {key}"
        assert entry[key] == data[key]


@pytest.mark.asyncio
async def test_log_entry_is_valid_json(patch_log_file):
    from src.trade_logger import log_trade
    await log_trade({"profit_pct": "0.9", "dry_run": False})

    with open(TEST_LOG, "r") as f:
        for line in f:
            json.loads(line)  # Should not raise


def test_setup_logging():
    from src.trade_logger import setup_logging
    import logging
    setup_logging()
    root = logging.getLogger()
    assert root.level <= logging.INFO


@pytest.mark.asyncio
async def test_log_trade_handles_write_error():
    from src.trade_logger import log_trade
    with patch("src.trade_logger.LOG_FILE", "/nonexistent/path/log.jsonl"):
        # Should not raise — logs error instead
        await log_trade({"profit_pct": "0.7", "dry_run": True})


# --- Tests for new atomic two-leg helpers (ATOM-09) ---


@pytest.mark.asyncio
async def test_log_trade_leg_basic(patch_log_file):
    """log_trade_leg appends a JSONL line with entry_type='leg' and all fields."""
    from src.trade_logger import log_trade_leg
    await log_trade_leg(
        leg=1,
        sequence=100,
        hash="ABC",
        engine_result="tesSUCCESS",
        ledger_index=99000000,
        dry_run=False,
    )
    with open(TEST_LOG, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["entry_type"] == "leg"
    assert obj["leg"] == 1
    assert obj["sequence"] == 100
    assert obj["hash"] == "ABC"
    assert obj["engine_result"] == "tesSUCCESS"
    assert obj["ledger_index"] == 99000000
    assert obj["dry_run"] is False
    assert "timestamp" in obj
    assert "T" in obj["timestamp"]


@pytest.mark.asyncio
async def test_log_trade_leg_with_latency(patch_log_file):
    """log_trade_leg stores latency_from_leg1_ms as an int for leg 2."""
    from src.trade_logger import log_trade_leg
    await log_trade_leg(
        leg=2,
        sequence=101,
        hash="DEF",
        engine_result="tesSUCCESS",
        ledger_index=99000000,
        dry_run=False,
        latency_from_leg1_ms=450,
    )
    with open(TEST_LOG, "r") as f:
        obj = json.loads(f.readline())
    assert obj["leg"] == 2
    assert obj["latency_from_leg1_ms"] == 450
    assert isinstance(obj["latency_from_leg1_ms"], int)


@pytest.mark.asyncio
async def test_log_trade_leg_with_path_used(patch_log_file):
    """log_trade_leg stores path_used as a JSON-serializable list (Warning-5 field)."""
    from src.trade_logger import log_trade_leg
    path = [{"currency": "USD", "issuer": "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B"}]
    await log_trade_leg(
        leg=1,
        sequence=100,
        hash="GHI",
        engine_result="tesSUCCESS",
        ledger_index=99000000,
        dry_run=False,
        path_used=path,
    )
    with open(TEST_LOG, "r") as f:
        obj = json.loads(f.readline())
    assert "path_used" in obj
    assert isinstance(obj["path_used"], list)
    assert obj["path_used"][0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_log_trade_summary_basic(patch_log_file):
    """log_trade_summary appends a JSONL line with entry_type='summary'."""
    from src.trade_logger import log_trade_summary
    await log_trade_summary(
        outcome="both_legs_success",
        dry_run=False,
        profit_pct=Decimal("0.4"),
        net_profit_xrp=Decimal("0.02"),
    )
    with open(TEST_LOG, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["entry_type"] == "summary"
    assert obj["outcome"] == "both_legs_success"
    assert obj["dry_run"] is False
    # Decimal values must be serialized as strings
    assert obj["profit_pct"] == "0.4"
    assert obj["net_profit_xrp"] == "0.02"
    assert "timestamp" in obj


@pytest.mark.asyncio
async def test_log_trade_still_works(patch_log_file):
    """Existing log_trade helper is unchanged and still appends correctly."""
    from src.trade_logger import log_trade
    await log_trade({"profit_pct": "0.7", "dry_run": True})
    with open(TEST_LOG, "r") as f:
        obj = json.loads(f.readline())
    assert obj["profit_pct"] == "0.7"
    assert obj["dry_run"] is True
