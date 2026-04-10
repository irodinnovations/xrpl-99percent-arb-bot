"""Unit tests for src/backtester.py — TDD RED phase.

Tests for BacktestEngine (JSONL loading) and compute_report (metrics calculation).
Requirements: BACK-01, BACK-02
"""

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from src.backtester import BacktestEngine, BacktestReport, compute_report


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_trade(profit_pct: str, profit_ratio: str, dry_run: bool = True) -> dict:
    """Helper to build a minimal valid trade dict."""
    return {
        "timestamp": "2026-04-10T12:00:00+00:00",
        "profit_pct": profit_pct,
        "profit_ratio": profit_ratio,
        "input_xrp": "50.000000",
        "output_xrp": "50.425000",
        "simulated_output": "50.425000",
        "dry_run": dry_run,
        "simulation_result": "tesSUCCESS",
    }


def _write_jsonl(lines: list, path: str) -> None:
    """Write a list of dicts as JSONL to path."""
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


# ---------------------------------------------------------------------------
# Test 1: load_trades() parses JSONL into list of dicts, skipping malformed lines
# ---------------------------------------------------------------------------

def test_load_trades_parses_valid_lines():
    """Test 1: BacktestEngine.load_trades() parses JSONL lines into a list of trade dicts,
    skipping malformed lines. (BACK-01)"""
    trades = [
        _make_trade("0.8500", "0.0085"),
        _make_trade("1.2000", "0.0120"),
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for trade in trades:
            f.write(json.dumps(trade) + "\n")
        # Inject a malformed line
        f.write("{ this is not valid json }\n")
        tmp_path = f.name

    try:
        engine = BacktestEngine(log_file=tmp_path)
        result = engine.load_trades()
        assert len(result) == 2, f"Expected 2 valid trades, got {len(result)}"
        assert result[0]["profit_pct"] == "0.8500"
        assert result[1]["profit_pct"] == "1.2000"
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test 2: load_trades() with last_n=5 returns only the last 5 entries
# ---------------------------------------------------------------------------

def test_load_trades_last_n():
    """Test 2: BacktestEngine.load_trades() with last_n=5 returns only the last 5 entries."""
    trades = [_make_trade(f"{i:.4f}", f"0.00{i:02d}") for i in range(1, 11)]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for trade in trades:
            f.write(json.dumps(trade) + "\n")
        tmp_path = f.name

    try:
        engine = BacktestEngine(log_file=tmp_path, last_n=5)
        result = engine.load_trades()
        assert len(result) == 5, f"Expected 5 trades with last_n=5, got {len(result)}"
        # Should be the last 5 (entries 6-10)
        assert result[0]["profit_pct"] == "6.0000"
        assert result[-1]["profit_pct"] == "10.0000"
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test 3: compute_report() with 3 wins and 1 loss — win_rate, total_opportunities
# ---------------------------------------------------------------------------

def test_compute_report_win_rate():
    """Test 3: BacktestReport.compute() with 3 winning trades and 1 losing trade
    returns win_rate=Decimal('75.0'), total_opportunities=4."""
    trades = [
        _make_trade("0.8500", "0.0085"),   # win (profit_ratio > 0)
        _make_trade("1.2000", "0.0120"),   # win
        _make_trade("0.6500", "0.0065"),   # win
        _make_trade("-0.2000", "-0.0020"), # loss (profit_ratio <= 0)
    ]
    report = compute_report(trades)
    assert report.total_opportunities == Decimal("4"), f"Expected 4, got {report.total_opportunities}"
    assert report.win_rate == Decimal("75.0"), f"Expected 75.0, got {report.win_rate}"
    assert report.profitable_count == Decimal("3")
    assert report.losing_count == Decimal("1")


# ---------------------------------------------------------------------------
# Test 4: compute_report() calculates correct avg_profit, max_profit, max_loss
# ---------------------------------------------------------------------------

def test_compute_report_profit_metrics():
    """Test 4: BacktestReport.compute() calculates correct avg_profit, max_profit, max_loss
    from Decimal strings."""
    trades = [
        _make_trade("1.0000", "0.0100"),
        _make_trade("2.0000", "0.0200"),
        _make_trade("-0.5000", "-0.0050"),
    ]
    report = compute_report(trades)
    # avg = (1.0 + 2.0 + (-0.5)) / 3 = 2.5 / 3 = 0.8333...
    expected_avg = (Decimal("1.0000") + Decimal("2.0000") + Decimal("-0.5000")) / Decimal("3")
    assert report.avg_profit == expected_avg, f"Expected avg {expected_avg}, got {report.avg_profit}"
    assert report.max_profit == Decimal("2.0000"), f"Expected max_profit 2.0000, got {report.max_profit}"
    assert report.max_loss == Decimal("-0.5000"), f"Expected max_loss -0.5000, got {report.max_loss}"


# ---------------------------------------------------------------------------
# Test 5: compute_report() with empty trades list returns all-zero report
# ---------------------------------------------------------------------------

def test_compute_report_empty_trades():
    """Test 5: BacktestReport.compute() with empty trades list returns all-zero report without error."""
    report = compute_report([])
    assert report.win_rate == Decimal("0")
    assert report.total_opportunities == Decimal("0")
    assert report.avg_profit == Decimal("0")
    assert report.max_profit == Decimal("0")
    assert report.max_loss == Decimal("0")
    assert report.profitable_count == Decimal("0")
    assert report.losing_count == Decimal("0")
    assert report.profit_buckets == {}


# ---------------------------------------------------------------------------
# Test 6: profit_distribution() returns dict of bucket counts
# ---------------------------------------------------------------------------

def test_compute_report_profit_distribution():
    """Test 6: BacktestReport.profit_distribution() returns a dict of bucket counts
    (e.g., '0.0-0.5': 2, '0.5-1.0': 1)."""
    trades = [
        _make_trade("0.3000", "0.0030"),   # 0.0-0.5
        _make_trade("0.4000", "0.0040"),   # 0.0-0.5
        _make_trade("0.7000", "0.0070"),   # 0.5-1.0
        _make_trade("1.5000", "0.0150"),   # 1.0-2.0
        _make_trade("2.5000", "0.0250"),   # 2.0+
        _make_trade("-0.3000", "-0.0030"), # <0
    ]
    report = compute_report(trades)
    buckets = report.profit_buckets
    assert buckets.get("<0") == 1, f"Expected <0 bucket=1, got {buckets.get('<0')}"
    assert buckets.get("0.0-0.5") == 2, f"Expected 0.0-0.5 bucket=2, got {buckets.get('0.0-0.5')}"
    assert buckets.get("0.5-1.0") == 1, f"Expected 0.5-1.0 bucket=1, got {buckets.get('0.5-1.0')}"
    assert buckets.get("1.0-2.0") == 1, f"Expected 1.0-2.0 bucket=1, got {buckets.get('1.0-2.0')}"
    assert buckets.get("2.0+") == 1, f"Expected 2.0+ bucket=1, got {buckets.get('2.0+')}"


# ---------------------------------------------------------------------------
# Test 7: load_trades() on nonexistent file returns empty list
# ---------------------------------------------------------------------------

def test_load_trades_nonexistent_file():
    """Test 7: load_trades() on a nonexistent file returns empty list and logs a warning."""
    engine = BacktestEngine(log_file="/nonexistent/path/to/missing_file.jsonl")
    result = engine.load_trades()
    assert result == [], f"Expected empty list for missing file, got {result}"
