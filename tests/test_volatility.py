"""Tests for the volatility tracker's book_changes parser.

These tests verify the parser handles the real-world XRPL book_changes
message formats, including the "XRP_drops" sentinel and the
"ISSUER/CURRENCY" IOU format rippled actually sends.
"""

from decimal import Decimal

from src.volatility import VolatilityTracker, _is_xrp_side, _extract_currency_code


def test_is_xrp_side_accepts_both_sentinels():
    assert _is_xrp_side("XRP") is True
    assert _is_xrp_side("XRP_drops") is True
    assert _is_xrp_side("USD") is False
    assert _is_xrp_side("") is False


def test_extract_currency_code_issuer_first():
    """Current rippled format: ISSUER/CURRENCY."""
    code = _extract_currency_code(
        "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B/USD"
    )
    assert code == "USD"


def test_extract_currency_code_issuer_first_hex_currency():
    """Hex-encoded currency (>3 chars) still extracts cleanly."""
    code = _extract_currency_code(
        "rGm7WCVp7WDkkdJM1cPzcrdCgr3cn5EzF1/5553444300000000000000000000000000000000"
    )
    assert code == "5553444300000000000000000000000000000000"


def test_extract_currency_code_currency_first_fallback():
    """Older docs format: CURRENCY/ISSUER — still works."""
    code = _extract_currency_code("USD/rhub8VRN55s94qWKDv6jmDy1pUyrFNpjAX")
    assert code == "USD"


def test_parser_records_change_real_rippled_format():
    """End-to-end: real rippled book_changes format produces a recorded change."""
    tracker = VolatilityTracker()

    msg = {
        "type": "bookChanges",
        "ledger_index": 103626500,
        "changes": [
            {
                "currency_a": "XRP_drops",
                "currency_b": "rvYAfWj5gh67oV6fW32ZzP3Aw4Eubs59B/USD",
                "volume_a": "100000000",
                "volume_b": "50.2",
                "open": "0.0000005",
                "close": "0.00000055",
                "high": "0.00000055",
                "low": "0.0000005",
            }
        ],
    }

    tracker.process_book_changes_message(msg)

    diag = tracker.get_diagnostics()
    assert diag["msgs_processed"] == 1
    assert diag["changes_recorded"] == 1
    assert diag["currencies_tracked"] == 1


def test_parser_rejects_malformed_entries_gracefully():
    """A mix of valid and malformed entries: only valid ones recorded."""
    tracker = VolatilityTracker()

    msg = {
        "type": "bookChanges",
        "changes": [
            {"currency_a": "XRP_drops", "currency_b": "rIssuer.../USD", "open": "bad", "close": "1"},
            {"currency_a": "XRP_drops", "currency_b": "rvYAfWj5.../USD", "open": "1", "close": "1.01"},
            {"open": "1", "close": "1.01"},  # no currencies
        ],
    }

    tracker.process_book_changes_message(msg)
    diag = tracker.get_diagnostics()
    assert diag["msgs_processed"] == 1
    assert diag["changes_recorded"] == 1  # only the middle one


def test_parser_handles_empty_changes():
    tracker = VolatilityTracker()
    tracker.process_book_changes_message({"type": "bookChanges", "changes": []})
    diag = tracker.get_diagnostics()
    assert diag["msgs_processed"] == 1
    assert diag["changes_recorded"] == 0


def test_volatility_populates_after_enough_observations():
    """After ≥3 observations, get_volatility returns a non-zero value."""
    tracker = VolatilityTracker()
    for open_r, close_r in [("1.0", "1.01"), ("1.01", "1.02"), ("1.02", "1.005")]:
        tracker.process_book_changes_message({
            "type": "bookChanges",
            "changes": [{
                "currency_a": "XRP_drops",
                "currency_b": "rIssuerFakeAddrXXXXXXXXXXXXXXX/USD",
                "open": open_r, "close": close_r,
            }],
        })

    vol = tracker.get_volatility("USD")
    assert vol > Decimal("0")
