"""Tests for PathFinder — request building, response parsing, multi-tier scanning."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from src.pathfinder import PathFinder, Opportunity, DROPS_PER_XRP, _deduplicate_opportunities


@pytest.fixture
def mock_connection():
    conn = MagicMock()
    conn.send_request = AsyncMock()
    return conn


@pytest.fixture
def pathfinder(mock_connection):
    return PathFinder(connection=mock_connection, wallet_address="rTestAddress123")


def test_build_path_request(pathfinder):
    req = pathfinder.build_path_request(Decimal("5"))
    assert req.source_account == "rTestAddress123"
    assert req.destination_account == "rTestAddress123"
    assert req.destination_amount == "5000000"
    assert req.source_currencies == [{"currency": "XRP"}]


def test_parse_alternatives_profitable(pathfinder):
    # source_amount < destination_amount means profit
    # input_xrp = 5, source pays 4.9 XRP = profit
    response = {
        "alternatives": [
            {
                "source_amount": "4900000",  # 4.9 XRP in drops
                "paths_computed": [["path1"]],
            }
        ]
    }
    opps = pathfinder.parse_alternatives(response, Decimal("5"), Decimal("0"))
    assert len(opps) == 1
    assert isinstance(opps[0], Opportunity)
    assert isinstance(opps[0].input_xrp, Decimal)
    assert isinstance(opps[0].profit_pct, Decimal)
    assert opps[0].output_xrp == Decimal("5")
    assert opps[0].input_xrp == Decimal("4.9")


def test_parse_alternatives_unprofitable(pathfinder):
    # source_amount nearly equals destination — no profit after fees
    response = {
        "alternatives": [
            {
                "source_amount": "4999000",  # 4.999 XRP — tiny margin
                "paths_computed": [],
            }
        ]
    }
    opps = pathfinder.parse_alternatives(response, Decimal("5"), Decimal("0"))
    assert len(opps) == 0


def test_parse_alternatives_no_alternatives_key(pathfinder):
    opps = pathfinder.parse_alternatives({}, Decimal("5"))
    assert opps == []


def test_parse_alternatives_none_response(pathfinder):
    opps = pathfinder.parse_alternatives(None, Decimal("5"))
    assert opps == []


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


# --- Multi-tier scanning tests ---


@pytest.mark.asyncio
async def test_scan_probes_all_tiers(pathfinder, mock_connection):
    """scan() should call ripple_path_find once per tier."""
    mock_connection.send_request.return_value = {"alternatives": []}

    tiers = [Decimal("0.01"), Decimal("0.05"), Decimal("0.10")]
    await pathfinder.scan(Decimal("100"), position_tiers=tiers)

    assert mock_connection.send_request.call_count == 3


@pytest.mark.asyncio
async def test_scan_uses_correct_amounts_per_tier(pathfinder, mock_connection):
    """Each tier should probe with balance * tier percentage."""
    mock_connection.send_request.return_value = {"alternatives": []}

    tiers = [Decimal("0.01"), Decimal("0.05"), Decimal("0.10")]
    await pathfinder.scan(Decimal("100"), position_tiers=tiers)

    # Check the destination_amount in each request (1, 5, 10 XRP in drops)
    calls = mock_connection.send_request.call_args_list
    amounts = [call[0][0].destination_amount for call in calls]
    assert amounts == ["1000000", "5000000", "10000000"]


@pytest.mark.asyncio
async def test_scan_merges_opportunities_across_tiers(pathfinder, mock_connection):
    """Opportunities from different tiers with different paths should all be returned."""
    response_a = {
        "alternatives": [{
            "source_amount": "950000",  # 0.95 XRP for 1 XRP out
            "paths_computed": [["pathA"]],
        }]
    }
    response_b = {"alternatives": []}
    response_c = {
        "alternatives": [{
            "source_amount": "9500000",  # 9.5 XRP for 10 XRP out
            "paths_computed": [["pathC"]],
        }]
    }
    mock_connection.send_request.side_effect = [response_a, response_b, response_c]

    tiers = [Decimal("0.01"), Decimal("0.05"), Decimal("0.10")]
    opps = await pathfinder.scan(Decimal("100"), position_tiers=tiers)

    assert len(opps) == 2
    # Both unique paths kept
    paths_found = {str(o.paths) for o in opps}
    assert len(paths_found) == 2


@pytest.mark.asyncio
async def test_scan_single_tier_backwards_compatible(pathfinder, mock_connection):
    """scan() with a single tier should work like the old behavior."""
    mock_connection.send_request.return_value = {"alternatives": []}

    await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.05")])

    assert mock_connection.send_request.call_count == 1
    call = mock_connection.send_request.call_args[0][0]
    assert call.destination_amount == "5000000"


# --- Deduplication tests ---


def test_deduplicate_keeps_highest_profit():
    """When two tiers find the same path, keep the one with higher profit ratio."""
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
    """Opportunities with different paths are not deduplicated."""
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
    """Single opportunity passes through unchanged."""
    opp = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[], source_currency="XRP",
    )
    result = _deduplicate_opportunities([opp])
    assert len(result) == 1


def test_deduplicate_empty_list():
    """Empty list returns empty list."""
    assert _deduplicate_opportunities([]) == []
