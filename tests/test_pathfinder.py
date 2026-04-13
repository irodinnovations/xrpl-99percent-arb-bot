"""Tests for PathFinder — book_offers rate discovery, spread checks, scanning."""

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
    assert mock_connection.send_request.call_count == 1


@pytest.mark.asyncio
async def test_fetch_trust_lines_failure_returns_stale(pathfinder, mock_connection):
    pathfinder._trust_lines = [{"currency": "USD", "account": "rTest"}]
    pathfinder._trust_lines_ts = 0
    mock_connection.send_request.return_value = None
    lines = await pathfinder._fetch_trust_lines()
    assert len(lines) == 1


# --- Buy rate (ask) from book_offers ---


@pytest.mark.asyncio
async def test_get_buy_rate_success(pathfinder, mock_connection):
    """Buy book: taker gets IOU, pays XRP drops."""
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
            "TakerPays": "74800000",  # 74.8 XRP in drops
        }]
    }
    rate = await pathfinder._get_buy_rate("USD", "rBitstamp")
    # 74.8 XRP / 100 USD = 0.748 XRP per USD
    assert rate == Decimal("74800000") / DROPS_PER_XRP / Decimal("100")
    assert rate == Decimal("0.748")


@pytest.mark.asyncio
async def test_get_buy_rate_empty_book(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"offers": []}
    rate = await pathfinder._get_buy_rate("USD", "rBitstamp")
    assert rate is None


@pytest.mark.asyncio
async def test_get_buy_rate_connection_failure(pathfinder, mock_connection):
    mock_connection.send_request.return_value = None
    rate = await pathfinder._get_buy_rate("USD", "rBitstamp")
    assert rate is None


@pytest.mark.asyncio
async def test_get_buy_rate_unexpected_format(pathfinder, mock_connection):
    """If TakerGets is XRP drops instead of IOU dict, return None."""
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": "1000000",
            "TakerPays": "500000",
        }]
    }
    rate = await pathfinder._get_buy_rate("USD", "rBitstamp")
    assert rate is None


# --- Sell rate (bid) from book_offers ---


@pytest.mark.asyncio
async def test_get_sell_rate_success(pathfinder, mock_connection):
    """Sell book: taker gets XRP drops, pays IOU."""
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": "74100000",  # 74.1 XRP in drops
            "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        }]
    }
    rate = await pathfinder._get_sell_rate("USD", "rBitstamp")
    # 74.1 XRP / 100 USD = 0.741 XRP per USD
    assert rate == Decimal("74100000") / DROPS_PER_XRP / Decimal("100")
    assert rate == Decimal("0.741")


@pytest.mark.asyncio
async def test_get_sell_rate_empty_book(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"offers": []}
    rate = await pathfinder._get_sell_rate("USD", "rBitstamp")
    assert rate is None


@pytest.mark.asyncio
async def test_get_sell_rate_unexpected_format(pathfinder, mock_connection):
    """If TakerGets is IOU dict instead of XRP drops, return None."""
    mock_connection.send_request.return_value = {
        "offers": [{
            "TakerGets": {"currency": "USD", "issuer": "rTest", "value": "100"},
            "TakerPays": {"currency": "EUR", "issuer": "rTest", "value": "90"},
        }]
    }
    rate = await pathfinder._get_sell_rate("USD", "rBitstamp")
    assert rate is None


# --- Path construction ---


def test_build_path():
    path = PathFinder._build_path("USD", "rBitstamp123")
    assert len(path) == 1
    assert len(path[0]) == 1
    step = path[0][0]
    assert step["currency"] == "USD"
    assert step["issuer"] == "rBitstamp123"
    assert step["type"] == 48


# --- Spread check (pure math, no RPC) ---


def test_check_spread_profitable(pathfinder):
    """Positive spread: sell rate > buy rate by enough to clear threshold."""
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.700"),   # Ask: 0.70 XRP per USD
        sell_rate=Decimal("0.750"),  # Bid: 0.75 XRP per USD
        position_xrp=Decimal("10"),
        volatility_factor=Decimal("0"),
    )
    assert opp is not None
    assert opp.input_xrp == Decimal("10")
    # output = 10 * 0.75 / 0.70 = 10.714...
    assert opp.output_xrp > Decimal("10")
    assert opp.profit_ratio > Decimal("0")


def test_check_spread_no_spread(pathfinder):
    """No spread: sell rate <= buy rate."""
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.748"),
        sell_rate=Decimal("0.741"),  # Bid < Ask
        position_xrp=Decimal("10"),
        volatility_factor=Decimal("0"),
    )
    assert opp is None


def test_check_spread_below_threshold(pathfinder):
    """Tiny spread: positive but below profit threshold after fees/slippage."""
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.7480"),
        sell_rate=Decimal("0.7485"),  # Barely above ask
        position_xrp=Decimal("10"),
        volatility_factor=Decimal("0"),
    )
    assert opp is None


def test_check_spread_correct_path(pathfinder):
    """Opportunity should contain the correct IOU routing path."""
    opp = pathfinder._check_spread(
        currency="USD", issuer="rBitstamp",
        buy_rate=Decimal("0.700"),
        sell_rate=Decimal("0.760"),
        position_xrp=Decimal("10"),
        volatility_factor=Decimal("0"),
    )
    assert opp is not None
    assert opp.paths == PathFinder._build_path("USD", "rBitstamp")


# --- Full scan ---


@pytest.mark.asyncio
async def test_scan_queries_both_books_per_iou(pathfinder, mock_connection):
    """scan() should make 2 book_offers calls per IOU (buy book + sell book)."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
        {"currency": "EUR", "account": "rGateHub"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # All books empty
    mock_connection.send_request.return_value = {"offers": []}

    await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])

    # 2 IOUs x 2 books = 4 calls
    assert mock_connection.send_request.call_count == 4


@pytest.mark.asyncio
async def test_scan_returns_opportunities(pathfinder, mock_connection):
    """Profitable spread creates an opportunity."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    # Buy book (ask): 0.70 XRP per USD
    buy_book = {
        "offers": [{
            "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
            "TakerPays": "70000000",
        }]
    }
    # Sell book (bid): 0.76 XRP per USD — nice spread
    sell_book = {
        "offers": [{
            "TakerGets": "76000000",
            "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        }]
    }
    mock_connection.send_request.side_effect = [buy_book, sell_book]

    opps = await pathfinder.scan(Decimal("100"), position_tiers=[Decimal("0.01")])
    assert len(opps) == 1
    assert opps[0].input_xrp == Decimal("1")
    # output = 1 * 0.76 / 0.70 = 1.0857...
    assert opps[0].output_xrp > Decimal("1.08")


@pytest.mark.asyncio
async def test_scan_tiers_share_rates(pathfinder, mock_connection):
    """Multiple tiers use the same rate discovery (no extra RPC calls)."""
    pathfinder._trust_lines = [
        {"currency": "USD", "account": "rBitstamp"},
    ]
    pathfinder._trust_lines_ts = 9999999999

    buy_book = {
        "offers": [{
            "TakerGets": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
            "TakerPays": "70000000",
        }]
    }
    sell_book = {
        "offers": [{
            "TakerGets": "76000000",
            "TakerPays": {"currency": "USD", "issuer": "rBitstamp", "value": "100"},
        }]
    }
    mock_connection.send_request.side_effect = [buy_book, sell_book]

    tiers = [Decimal("0.01"), Decimal("0.05"), Decimal("0.10")]
    opps = await pathfinder.scan(Decimal("100"), position_tiers=tiers)

    # Only 2 RPC calls (one buy book, one sell book) for 3 tiers
    assert mock_connection.send_request.call_count == 2
    # All tiers should find the same spread — dedup keeps best
    assert len(opps) >= 1


@pytest.mark.asyncio
async def test_scan_no_trust_lines(pathfinder, mock_connection):
    mock_connection.send_request.return_value = {"lines": []}
    pathfinder._trust_lines_ts = 0
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
