"""Tests for two-leg PathFinder — probe helpers, round-trip checks, scanning."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from src.pathfinder import PathFinder, Opportunity, DROPS_PER_XRP, _deduplicate_opportunities


@pytest.fixture
def mock_connection():
    conn = MagicMock()
    conn.send_request = AsyncMock()
    return conn


@pytest.fixture
def pathfinder(mock_connection):
    return PathFinder(connection=mock_connection, wallet_address="rTestAddress123")


# --- Opportunity dataclass ---


def test_opportunity_dataclass_fields():
    opp = Opportunity(
        input_xrp=Decimal("1"),
        output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"),
        profit_ratio=Decimal("0.007"),
        paths=[],
        source_currency="XRP",
    )
    assert opp.input_xrp == Decimal("1")
    assert opp.output_xrp == Decimal("1.01")
    assert opp.profit_pct == Decimal("0.7")
    assert opp.source_currency == "XRP"


# --- Trust line fetching ---


@pytest.mark.asyncio
async def test_fetch_trust_lines(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "lines": [
            {"currency": "USD", "account": "rBitstamp", "balance": "0", "limit": "1000000"},
            {"currency": "EUR", "account": "rGateHub", "balance": "0", "limit": "1000000"},
        ]
    }
    lines = await pathfinder._fetch_trust_lines()
    assert len(lines) == 2
    assert lines[0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_fetch_trust_lines_caches(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "lines": [{"currency": "USD", "account": "rBitstamp", "balance": "0", "limit": "1000000"}]
    }
    await pathfinder._fetch_trust_lines()
    await pathfinder._fetch_trust_lines()
    # Only one RPC call — second uses cache
    assert mock_connection.send_request.call_count == 1


@pytest.mark.asyncio
async def test_fetch_trust_lines_failure_returns_stale(pathfinder, mock_connection):
    # Populate cache first
    pathfinder._trust_lines = [{"currency": "USD", "account": "rTest"}]
    pathfinder._trust_lines_ts = 0  # Force cache miss
    mock_connection.send_request.return_value = None  # Failure
    lines = await pathfinder._fetch_trust_lines()
    assert len(lines) == 1  # Returns stale cache


# --- Buy probe ---


@pytest.mark.asyncio
async def test_probe_buy_cost_success(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "alternatives": [{
            "source_amount": "748000",  # 0.748 XRP in drops
            "paths_computed": [["somepath"]],
        }]
    }
    cost = await pathfinder._probe_buy_cost("USD", "rBitstamp", Decimal("1"))
    assert cost == Decimal("748000") / DROPS_PER_XRP
    assert cost == Decimal("0.748")


@pytest.mark.asyncio
async def test_probe_buy_cost_no_alternatives(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"alternatives": []}
    cost = await pathfinder._probe_buy_cost("USD", "rBitstamp", Decimal("1"))
    assert cost is None


@pytest.mark.asyncio
async def test_probe_buy_cost_iou_source_returns_none(pathfinder, mock_connection):
    """If source_amount is an IOU dict (not drops), return None."""
    mock_connection.send_request.return_value = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rTest", "value": "100"},
        }]
    }
    cost = await pathfinder._probe_buy_cost("USD", "rBitstamp", Decimal("1"))
    assert cost is None


@pytest.mark.asyncio
async def test_probe_buy_cost_connection_failure(pathfinder, mock_connection):
    mock_connection.send_request.return_value = None
    cost = await pathfinder._probe_buy_cost("USD", "rBitstamp", Decimal("1"))
    assert cost is None


# --- Sell probe ---


@pytest.mark.asyncio
async def test_probe_sell_cost_success(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.35"},
            "paths_computed": [["somepath"]],
        }]
    }
    cost = await pathfinder._probe_sell_cost("USD", "rBitstamp", Decimal("1"))
    assert cost == Decimal("1.35")


@pytest.mark.asyncio
async def test_probe_sell_cost_no_alternatives(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"alternatives": []}
    cost = await pathfinder._probe_sell_cost("USD", "rBitstamp", Decimal("1"))
    assert cost is None


@pytest.mark.asyncio
async def test_probe_sell_cost_drops_source_returns_none(pathfinder, mock_connection):
    """If source_amount is drops (not IOU dict), return None."""
    mock_connection.send_request.return_value = {
        "alternatives": [{
            "source_amount": "1000000",
        }]
    }
    cost = await pathfinder._probe_sell_cost("USD", "rBitstamp", Decimal("1"))
    assert cost is None


# --- Path construction ---


def test_build_path():
    path = PathFinder._build_path("USD", "rBitstamp123")
    assert len(path) == 1  # One path
    assert len(path[0]) == 1  # One step in the path
    step = path[0][0]
    assert step["currency"] == "USD"
    assert step["issuer"] == "rBitstamp123"
    assert step["type"] == 48


# --- Single IOU round-trip check ---


@pytest.mark.asyncio
async def test_check_iou_profitable(pathfinder, mock_connection):
    """Profitable round-trip: buy cost < sell yield."""
    # Sell probe: need 1.3 USD to get 1 XRP back
    sell_response = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.3"},
        }]
    }
    # Buy probe: 1.3 USD costs 0.95 XRP (less than the 1 XRP we'd get back)
    buy_response = {
        "alternatives": [{
            "source_amount": "950000",  # 0.95 XRP in drops
        }]
    }
    mock_connection.send_request.side_effect = [sell_response, buy_response]

    opp = await pathfinder._check_iou("USD", "rBitstamp", Decimal("1"), Decimal("0"))

    assert opp is not None
    assert opp.input_xrp == Decimal("0.95")
    assert opp.output_xrp == Decimal("1")
    assert opp.profit_ratio > Decimal("0")
    assert opp.paths == PathFinder._build_path("USD", "rBitstamp")


@pytest.mark.asyncio
async def test_check_iou_unprofitable(pathfinder, mock_connection):
    """No profit: buy cost >= sell yield."""
    sell_response = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.3"},
        }]
    }
    # Buy probe: 1.3 USD costs 1.01 XRP (more than the 1 XRP we'd get back)
    buy_response = {
        "alternatives": [{
            "source_amount": "1010000",  # 1.01 XRP
        }]
    }
    mock_connection.send_request.side_effect = [sell_response, buy_response]

    opp = await pathfinder._check_iou("USD", "rBitstamp", Decimal("1"), Decimal("0"))
    assert opp is None


@pytest.mark.asyncio
async def test_check_iou_sell_probe_fails(pathfinder, mock_connection):
    """If sell probe returns no path, skip this IOU."""
    mock_connection.send_request.return_value = {"alternatives": []}
    opp = await pathfinder._check_iou("USD", "rBitstamp", Decimal("1"), Decimal("0"))
    assert opp is None
    # Only one call made (sell probe), buy probe never called
    assert mock_connection.send_request.call_count == 1


@pytest.mark.asyncio
async def test_check_iou_buy_probe_fails(pathfinder, mock_connection):
    """If buy probe returns no path, skip."""
    sell_response = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.3"},
        }]
    }
    mock_connection.send_request.side_effect = [
        sell_response,
        {"alternatives": []},  # buy probe fails
    ]
    opp = await pathfinder._check_iou("USD", "rBitstamp", Decimal("1"), Decimal("0"))
    assert opp is None


@pytest.mark.asyncio
async def test_check_iou_below_threshold(pathfinder, mock_connection):
    """Marginal profit below threshold is rejected."""
    sell_response = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.3"},
        }]
    }
    # Very thin margin: 0.999 XRP cost for 1 XRP — profit < threshold
    buy_response = {
        "alternatives": [{"source_amount": "999000"}]
    }
    mock_connection.send_request.side_effect = [sell_response, buy_response]

    opp = await pathfinder._check_iou("USD", "rBitstamp", Decimal("1"), Decimal("0"))
    assert opp is None  # Below 0.6% threshold after fees/slippage


# --- Multi-tier scan ---


@pytest.mark.asyncio
async def test_scan_iterates_trust_lines_and_tiers(pathfinder, mock_connection):
    """scan() should probe each IOU x tier combination."""
    # Populate trust lines cache to avoid account_lines call
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
        {"currency": "EUR", "account": "rGateHub"},
    ]
    pathfinder._trust_lines_ts = 9999999999  # Far future = cached

    # All probes return no alternatives
    mock_connection.send_request.return_value = {"alternatives": []}

    tiers = [Decimal("0.01"), Decimal("0.05")]
    await pathfinder.scan(Decimal("100"), position_tiers=tiers)

    # 2 IOUs x 2 tiers = 4 sell probes, buy probes skipped (sell returns empty)
    # But sell probes return no alternatives so only 4 calls total
    assert mock_connection.send_request.call_count == 4


@pytest.mark.asyncio
async def test_scan_returns_opportunities(pathfinder, mock_connection):
    """scan() returns profitable opportunities across IOUs."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # Sell then buy for each tier
    sell_resp = {
        "alternatives": [{
            "source_amount": {"currency": "USD", "issuer": "rBitstamp", "value": "1.3"},
        }]
    }
    buy_resp = {
        "alternatives": [{"source_amount": "950000"}]  # 0.95 XRP for 1 XRP tier
    }
    # Only one tier: sell, buy
    mock_connection.send_request.side_effect = [sell_resp, buy_resp]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])
    assert len(opps) == 1
    assert opps[0].input_xrp == Decimal("0.95")
    assert opps[0].output_xrp == Decimal("1")


@pytest.mark.asyncio
async def test_scan_no_trust_lines(pathfinder, mock_connection):
    """scan() with no trust lines returns empty."""
    mock_connection.send_request.return_value = {"lines": []}
    pathfinder._trust_lines_ts = 0  # Force refresh

    opps = await pathfinder.scan(Decimal("100"))
    assert opps == []


# --- Deduplication tests ---


def test_deduplicate_keeps_highest_profit():
    opp_low = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[["same_path"]], source_currency="XRP",
    )
    opp_high = Opportunity(
        input_xrp=Decimal("5"), output_xrp=Decimal("5.06"),
        profit_pct=Decimal("0.9"), profit_ratio=Decimal("0.009"),
        paths=[["same_path"]], source_currency="XRP",
    )
    result = _deduplicate_opportunities([opp_low, opp_high])
    assert len(result) == 1
    assert result[0].profit_ratio == Decimal("0.009")


def test_deduplicate_keeps_different_paths():
    opp_a = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[["path_a"]], source_currency="XRP",
    )
    opp_b = Opportunity(
        input_xrp=Decimal("5"), output_xrp=Decimal("5.05"),
        profit_pct=Decimal("0.8"), profit_ratio=Decimal("0.008"),
        paths=[["path_b"]], source_currency="XRP",
    )
    result = _deduplicate_opportunities([opp_a, opp_b])
    assert len(result) == 2


def test_deduplicate_single_item():
    opp = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[], source_currency="XRP",
    )
    result = _deduplicate_opportunities([opp])
    assert len(result) == 1


def test_deduplicate_empty_list():
    assert _deduplicate_opportunities([]) == []
