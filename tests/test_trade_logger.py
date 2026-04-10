"""Tests for JSONL trade logger."""

import json
import os
import pytest
import tempfile
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
