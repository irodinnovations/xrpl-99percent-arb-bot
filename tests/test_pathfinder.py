"""Tests for PathFinder — request building and response parsing."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from src.pathfinder import PathFinder, Opportunity, DROPS_PER_XRP


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
