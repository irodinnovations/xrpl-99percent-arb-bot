"""Tests for PathFinder — CLOB rates, AMM rates, cross-issuer, scanning."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from src.pathfinder import (
    PathFinder, Opportunity, IouRates, DROPS_PER_XRP, _deduplicate_opportunities,
)


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
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[], source_currency="XRP",
    )
    assert opp.input_xrp == Decimal("1")
    assert opp.source_currency == "XRP"


# --- IouRates ---


def test_iou_rates_best_buy_picks_cheapest():
    rates = IouRates(
        currency="USD", issuer="rTest",
        clob_buy=Decimal("0.750"), amm_buy=Decimal("0.740"),
    )
    assert rates.best_buy == Decimal("0.740")  # AMM is cheaper


def test_iou_rates_best_sell_picks_highest():
    rates = IouRates(
        currency="USD", issuer="rTest",
        clob_sell=Decimal("0.730"), amm_sell=Decimal("0.735"),
    )
    assert rates.best_sell == Decimal("0.735")  # AMM pays more


def test_iou_rates_none_when_missing():
    rates = IouRates(currency="USD", issuer="rTest")
    assert rates.best_buy is None
    assert rates.best_sell is None


def test_iou_rates_single_venue():
    rates = IouRates(currency="USD", issuer="rTest", clob_buy=Decimal("0.750"))
    assert rates.best_buy == Decimal("0.750")
    assert rates.best_sell is None


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


@pytest.mark.asyncio
async def test_fetch_trust_lines_caches(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "lines": [{"currency": "USD", "account": "rBitstamp", "balance": "0", "limit": "1000000"}]
    }
    await pathfinder._fetch_trust_lines()
    await pathfinder._fetch_trust_lines()
    assert mock_connection.send_request.call_count == 1


# --- CLOB buy rate ---


@pytest.mark.asyncio
async def test_get_buy_rate_success(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
            "TakerPays": "74800000",
        }]
    }
    rate = await pathfinder._get_buy_rate("USD", "rBitstamp")
    assert rate == Decimal("0.748")


@pytest.mark.asyncio
async def test_get_buy_rate_empty_book(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"offers": []}
    assert await pathfinder._get_buy_rate("USD", "rBitstamp") is None


@pytest.mark.asyncio
async def test_get_buy_rate_connection_failure(pathfinder, mock_connection):
    mock_connection.send_request.return_value = None
    assert await pathfinder._get_buy_rate("USD", "rBitstamp") is None


@pytest.mark.asyncio
async def test_get_buy_rate_unexpected_format(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "offers": [{"TakerGets": "1000000", "TakerPays": "500000"}]
    }
    assert await pathfinder._get_buy_rate("USD", "rBitstamp") is None


# --- CLOB sell rate ---


@pytest.mark.asyncio
async def test_get_sell_rate_success(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": "74100000",
            "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        }]
    }
    rate = await pathfinder._get_sell_rate("USD", "rBitstamp")
    assert rate == Decimal("0.741")


@pytest.mark.asyncio
async def test_get_sell_rate_empty_book(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"offers": []}
    assert await pathfinder._get_sell_rate("USD", "rBitstamp") is None


# --- AMM rates ---


@pytest.mark.asyncio
async def test_get_amm_rates_success(pathfinder, mock_connection):
    """AMM with XRP as amount (drops) and IOU as amount2 (dict)."""
    mock_connection.send_request.return_value = {
        "amm": {
            "amount": "100000000000",  # 100,000 XRP in drops
            "amount2": {"currency": "USD", "issuer": "rBitstamp", "value": "135000"},
            "trading_fee": 500,  # 0.5%
        }
    }
    result = await pathfinder._get_amm_rates("USD", "rBitstamp")
    assert result is not None
    buy_rate, sell_rate = result
    # Mid price = 100000 XRP / 135000 USD ≈ 0.7407 XRP per USD
    # Buy = mid / (1 - 0.005) = 0.7407 / 0.995 ≈ 0.7444
    # Sell = mid * (1 - 0.005) = 0.7407 * 0.995 ≈ 0.7370
    assert buy_rate > sell_rate  # Ask > bid
    assert Decimal("0.74") < buy_rate < Decimal("0.75")
    assert Decimal("0.73") < sell_rate < Decimal("0.74")


@pytest.mark.asyncio
async def test_get_amm_rates_xrp_as_amount2(pathfinder, mock_connection):
    """AMM with XRP as amount2 (drops) and IOU as amount (dict)."""
    mock_connection.send_request.return_value = {
        "amm": {
            "amount": {"currency": "USD", "issuer": "rBitstamp", "value": "135000"},
            "amount2": "100000000000",
            "trading_fee": 500,
        }
    }
    result = await pathfinder._get_amm_rates("USD", "rBitstamp")
    assert result is not None
    buy_rate, sell_rate = result
    assert buy_rate > sell_rate


@pytest.mark.asyncio
async def test_get_amm_rates_no_pool(pathfinder, mock_connection):
    mock_connection.send_request.return_value = None
    assert await pathfinder._get_amm_rates("USD", "rBitstamp") is None


@pytest.mark.asyncio
async def test_get_amm_rates_no_amm_key(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"error": "actNotFound"}
    assert await pathfinder._get_amm_rates("USD", "rBitstamp") is None


# --- Path construction ---


def test_build_path_single_issuer():
    path = PathFinder._build_path("USD", "rBitstamp")
    assert len(path) == 1
    assert len(path[0]) == 1
    assert path[0][0]["currency"] == "USD"
    assert path[0][0]["type"] == 48


def test_build_cross_issuer_path():
    path = PathFinder._build_cross_issuer_path("USD", "rGateHub", "rBitstamp")
    assert len(path) == 1
    assert len(path[0]) == 2  # Two hops
    assert path[0][0]["issuer"] == "rGateHub"   # Buy from cheap
    assert path[0][1]["issuer"] == "rBitstamp"  # Sell to expensive


# --- Spread check ---


def test_check_spread_profitable(pathfinder):
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.700"), sell_rate=Decimal("0.750"),
        position_xrp=Decimal("10"), volatility_factor=Decimal("0"),
    )
    assert opp is not None
    assert opp.input_xrp == Decimal("10")
    assert opp.output_xrp > Decimal("10")


def test_check_spread_no_spread(pathfinder):
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.748"), sell_rate=Decimal("0.741"),
        position_xrp=Decimal("10"), volatility_factor=Decimal("0"),
    )
    assert opp is None


def test_check_spread_below_threshold(pathfinder):
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.7480"), sell_rate=Decimal("0.7485"),
        position_xrp=Decimal("10"), volatility_factor=Decimal("0"),
    )
    assert opp is None


def test_check_spread_custom_path(pathfinder):
    """Custom path (e.g., cross-issuer) should be used in the opportunity."""
    custom_path = PathFinder._build_cross_issuer_path("USD", "rA", "rB")
    opp = pathfinder._check_spread(
        currency="USD", issuer="rA",
        buy_rate=Decimal("0.700"), sell_rate=Decimal("0.760"),
        position_xrp=Decimal("10"), volatility_factor=Decimal("0"),
        paths=custom_path,
    )
    assert opp is not None
    assert opp.paths == custom_path


# --- Full scan: same-issuer ---


@pytest.mark.asyncio
async def test_scan_calls_per_iou(pathfinder, mock_connection):
    """scan() makes 3 calls per IOU: buy book, sell book, amm_info."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # buy book, sell book return empty; amm_info returns no pool
    mock_connection.send_request.return_value = {"offers": []}

    await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])

    # 1 IOU x 3 calls = 3
    assert mock_connection.send_request.call_count == 3


@pytest.mark.asyncio
async def test_scan_same_issuer_opportunity(pathfinder, mock_connection):
    pathfinder._trust_lines = [{"currency": "USD", "account": "rBitstamp"}]
    pathfinder._trust_lines_ts = 9999999999

    buy_book = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        "TakerPays": "70000000",
    }]}
    sell_book = {"offers": [{
        "TakerGets": "76000000",
        "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
    }]}
    amm_none = {"error": "actNotFound"}  # No AMM pool
    mock_connection.send_request.side_effect = [buy_book, sell_book, amm_none]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])
    assert len(opps) == 1
    assert opps[0].output_xrp > Decimal("1.08")


@pytest.mark.asyncio
async def test_scan_amm_improves_rate(pathfinder, mock_connection):
    """AMM with better sell rate should create opportunity even if CLOB doesn't."""
    pathfinder._trust_lines = [{"currency": "USD", "account": "rBitstamp"}]
    pathfinder._trust_lines_ts = 9999999999

    # CLOB: ask 0.740, bid 0.730 → negative spread
    buy_book = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        "TakerPays": "74000000",
    }]}
    sell_book = {"offers": [{
        "TakerGets": "73000000",
        "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
    }]}
    # AMM: mid = 0.740, fee 0.1% → buy 0.7407, sell 0.7393
    # best_buy = min(CLOB 0.740, AMM 0.7407) = 0.740
    # best_sell = max(CLOB 0.730, AMM 0.7393) = 0.7393
    # Still negative spread (0.7393 < 0.740), so no opportunity
    amm_resp = {"amm": {
        "amount": "74000000000",  # 74000 XRP
        "amount2": {"currency": "USD", "issuer": "rBitstamp", "value": "100000"},
        "trading_fee": 100,  # 0.1%
    }}
    mock_connection.send_request.side_effect = [buy_book, sell_book, amm_resp]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])
    # AMM improved sell rate from 0.730 to 0.7393 but still < buy 0.740
    assert len(opps) == 0


# --- Full scan: cross-issuer ---


@pytest.mark.asyncio
async def test_scan_cross_issuer_opportunity(pathfinder, mock_connection):
    """Two USD issuers: buy from cheap, sell to expensive."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rGateHub"},
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # GateHub: ask 0.700 (cheap to buy)
    gh_buy = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rGateHub", "value": "100"},
        "TakerPays": "70000000",
    }]}
    gh_sell = {"offers": [{
        "TakerGets": "69000000",
        "TakerPays": {"currency": "USD", "issuer": "rGateHub", "value": "100"},
    }]}
    gh_amm = {"error": "actNotFound"}

    # Bitstamp: bid 0.760 (expensive to sell)
    bs_buy = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        "TakerPays": "77000000",
    }]}
    bs_sell = {"offers": [{
        "TakerGets": "76000000",
        "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
    }]}
    bs_amm = {"error": "actNotFound"}

    # Order: GateHub (buy, sell, amm), Bitstamp (buy, sell, amm)
    mock_connection.send_request.side_effect = [
        gh_buy, gh_sell, gh_amm,
        bs_buy, bs_sell, bs_amm,
    ]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])

    # Should find cross-issuer: buy GateHub 0.700, sell Bitstamp 0.760
    cross_opps = [o for o in opps if len(o.paths[0]) == 2]  # Two-hop paths
    assert len(cross_opps) >= 1
    assert cross_opps[0].paths[0][0]["issuer"] == "rGateHub"
    assert cross_opps[0].paths[0][1]["issuer"] == "rBitstamp"


@pytest.mark.asyncio
async def test_scan_cross_issuer_skips_same_issuer(pathfinder, mock_connection):
    """If cheapest buy and best sell are same issuer, skip cross-issuer."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rGateHub"},
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # GateHub: ask 0.700, bid 0.760 — best on both sides
    gh_buy = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rGateHub", "value": "100"},
        "TakerPays": "70000000",
    }]}
    gh_sell = {"offers": [{
        "TakerGets": "76000000",
        "TakerPays": {"currency": "USD", "issuer": "rGateHub", "value": "100"},
    }]}
    gh_amm = {"error": "actNotFound"}

    # Bitstamp: worse on both sides
    bs_buy = {"offers": [{
        "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        "TakerPays": "77000000",
    }]}
    bs_sell = {"offers": [{
        "TakerGets": "69000000",
        "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
    }]}
    bs_amm = {"error": "actNotFound"}

    mock_connection.send_request.side_effect = [
        gh_buy, gh_sell, gh_amm,
        bs_buy, bs_sell, bs_amm,
    ]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])

    # Cross-issuer skipped (same issuer rGateHub has best buy AND sell)
    # Only same-issuer GateHub opportunity should exist
    cross_opps = [o for o in opps if len(o.paths[0]) == 2]
    assert len(cross_opps) == 0


@pytest.mark.asyncio
async def test_scan_no_trust_lines(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"lines": []}
    pathfinder._trust_lines_ts = 0
    assert await pathfinder.scan(Decimal("100")) == []


# --- Deduplication ---


def test_deduplicate_keeps_highest_profit():
    opp_low = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[["same"]], source_currency="XRP",
    )
    opp_high = Opportunity(
        input_xrp=Decimal("5"), output_xrp=Decimal("5.06"),
        profit_pct=Decimal("0.9"), profit_ratio=Decimal("0.009"),
        paths=[["same"]], source_currency="XRP",
    )
    result = _deduplicate_opportunities([opp_low, opp_high])
    assert len(result) == 1
    assert result[0].profit_ratio == Decimal("0.009")


def test_deduplicate_keeps_different_paths():
    opp_a = Opportunity(
        input_xrp=Decimal("1"), output_xrp=Decimal("1.01"),
        profit_pct=Decimal("0.7"), profit_ratio=Decimal("0.007"),
        paths=[["a"]], source_currency="XRP",
    )
    opp_b = Opportunity(
        input_xrp=Decimal("5"), output_xrp=Decimal("5.05"),
        profit_pct=Decimal("0.8"), profit_ratio=Decimal("0.008"),
        paths=[["b"]], source_currency="XRP",
    )
    assert len(_deduplicate_opportunities([opp_a, opp_b])) == 2


def test_deduplicate_empty():
    assert _deduplicate_opportunities([]) == []
