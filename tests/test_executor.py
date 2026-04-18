"""Tests for TradeExecutor — two-leg Payment flow and helpers.

Covers:
- Transaction builders (_build_leg1_tx, _build_leg2_tx)
- IOU value formatting (_format_iou_value)
- Sim delivered-amount extraction (_extract_delivered_iou)
- Safety gates (circuit breaker, blacklist)
- Legacy multi-hop opportunities are skipped (no two-leg metadata)
- DRY_RUN paper trading with both-leg simulation
- Leg 1 sim failure aborts with no state
- Leg 2 sim failure aborts with no state
- Recovery stub fires when leg 1 validated but leg 2 fails
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.executor import (
    TradeExecutor,
    _build_leg1_tx,
    _build_leg2_tx,
    _format_iou_value,
    _extract_delivered_iou,
    _LEG1_SENDMAX_BUFFER,
    DROPS_PER_XRP,
)
from src.pathfinder import Opportunity
from src.simulator import SimResult


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


WALLET_ADDR = "rTestAddress1234567890abcdefgh"
BUY_ISSUER = "rBuy8888888888888888888888888"
SELL_ISSUER = "rSell7777777777777777777777777"


@pytest.fixture
def same_issuer_opp():
    """Same-issuer arbitrage opportunity (sell_issuer == buy_issuer)."""
    return Opportunity(
        input_xrp=Decimal("5"),
        output_xrp=Decimal("5.05"),
        profit_pct=Decimal("1.0"),
        profit_ratio=Decimal("0.01"),
        iou_currency="USD",
        buy_issuer=BUY_ISSUER,
        sell_issuer=BUY_ISSUER,
        iou_amount=Decimal("2.5"),
        paths=[],
        source_currency="XRP",
    )


@pytest.fixture
def cross_issuer_opp():
    """Cross-issuer arbitrage (different buy/sell issuers).

    input_xrp kept under MAX_TRADE_XRP_ABS (5.0 default) so tests don't
    trip the B5 absolute-cap guard; the absolute-cap behavior has its
    own dedicated test with an explicitly oversized opportunity.
    """
    return Opportunity(
        input_xrp=Decimal("3"),
        output_xrp=Decimal("3.036"),
        profit_pct=Decimal("1.2"),
        profit_ratio=Decimal("0.012"),
        iou_currency="USD",
        buy_issuer=BUY_ISSUER,
        sell_issuer=SELL_ISSUER,
        iou_amount=Decimal("1.5"),
        paths=[],
        source_currency="XRP",
    )


@pytest.fixture
def multihop_legacy_opp():
    """Legacy multi-hop opp from ripple_path_find — no two-leg metadata."""
    return Opportunity(
        input_xrp=Decimal("5"),
        output_xrp=Decimal("5.05"),
        profit_pct=Decimal("1.0"),
        profit_ratio=Decimal("0.01"),
        paths=[[{"currency": "USD", "issuer": BUY_ISSUER}]],
        source_currency="XRP",
    )


@pytest.fixture
def mock_circuit_breaker():
    cb = MagicMock()
    cb.is_halted.return_value = False
    cb.record_trade = MagicMock()
    return cb


@pytest.fixture
def mock_blacklist():
    bl = MagicMock()
    bl.is_blacklisted.return_value = False
    bl.is_route_blocked.return_value = False
    return bl


@pytest.fixture
def mock_wallet():
    w = MagicMock()
    w.address = WALLET_ADDR
    w.public_key = "ED0000000000000000000000000000000000000000000000000000000000000000"
    w.private_key = "ED0000000000000000000000000000000000000000000000000000000000000001"
    return w


def _leg1_sim_success(delivered_value: str = "2.5") -> SimResult:
    """Build a successful leg-1 SimResult with a delivered_amount populated.

    Populates both the typed `delivered_amount` field (used by the executor
    via SimResult.delivered_iou_value()) and the raw meta dict (preserved
    for any callers that still inspect the raw response).
    """
    delivered = {
        "currency": "USD",
        "issuer": BUY_ISSUER,
        "value": delivered_value,
    }
    return SimResult(
        success=True,
        result_code="tesSUCCESS",
        delivered_amount=delivered,
        raw={
            "engine_result": "tesSUCCESS",
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": delivered,
            },
        },
    )


def _leg2_sim_success() -> SimResult:
    return SimResult(
        success=True,
        result_code="tesSUCCESS",
        raw={
            "engine_result": "tesSUCCESS",
            "meta": {"TransactionResult": "tesSUCCESS"},
        },
    )


def _patch_autofill(executor: TradeExecutor, sequence: int = 100, ledger: int = 99999):
    """Replace the executor's autofill with a predictable coroutine."""
    executor._fetch_account_info = AsyncMock(return_value=(sequence, ledger))


# ===========================================================================
# Helper: _format_iou_value
# ===========================================================================


class TestFormatIouValue:
    def test_integer_preserves_significant_zeros(self):
        assert _format_iou_value(Decimal("100")) == "100"

    def test_trailing_zeros_after_decimal_stripped(self):
        assert _format_iou_value(Decimal("1.234000")) == "1.234"

    def test_simple_decimal(self):
        assert _format_iou_value(Decimal("0.5")) == "0.5"

    def test_zero(self):
        assert _format_iou_value(Decimal("0")) == "0"

    def test_no_scientific_notation_for_small_values(self):
        # Decimal("0.00000001") would normalize to "1E-8" — we must avoid that
        formatted = _format_iou_value(Decimal("0.00000001"))
        assert "E" not in formatted.upper()
        assert Decimal(formatted) == Decimal("0.00000001")

    def test_large_precise_value(self):
        assert _format_iou_value(Decimal("1234567.89012345")) == "1234567.89012345"


# ===========================================================================
# Helper: _extract_delivered_iou
# ===========================================================================


class TestExtractDeliveredIou:
    def test_iou_delivered_returns_decimal(self):
        raw = {
            "meta": {
                "delivered_amount": {
                    "currency": "USD",
                    "issuer": BUY_ISSUER,
                    "value": "2.345678",
                }
            }
        }
        assert _extract_delivered_iou(raw) == Decimal("2.345678")

    def test_missing_meta_returns_none(self):
        assert _extract_delivered_iou({}) is None

    def test_missing_delivered_amount_returns_none(self):
        assert _extract_delivered_iou({"meta": {}}) is None

    def test_xrp_string_delivery_returns_none(self):
        # Leg 1 should never deliver XRP; if it does, caller treats as bad.
        raw = {"meta": {"delivered_amount": "5000000"}}
        assert _extract_delivered_iou(raw) is None

    def test_zero_value_returns_none(self):
        raw = {"meta": {"delivered_amount": {"value": "0"}}}
        assert _extract_delivered_iou(raw) is None

    def test_malformed_value_returns_none(self):
        raw = {"meta": {"delivered_amount": {"value": "not-a-number"}}}
        assert _extract_delivered_iou(raw) is None

    def test_none_input_returns_none(self):
        assert _extract_delivered_iou(None) is None


# ===========================================================================
# Helper: _build_leg1_tx
# ===========================================================================


class TestBuildLeg1Tx:
    def test_basic_shape(self, same_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        assert tx["TransactionType"] == "Payment"
        assert tx["Account"] == WALLET_ADDR
        assert tx["Destination"] == WALLET_ADDR

    def test_amount_is_iou_dict(self, same_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        assert tx["Amount"] == {
            "currency": "USD",
            "issuer": BUY_ISSUER,
            "value": "2.5",
        }

    def test_sendmax_has_one_percent_buffer(self, same_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        # input_xrp = 5, buffer = 1% → 5.05 XRP → 5_050_000 drops
        expected_drops = int(
            Decimal("5") * DROPS_PER_XRP * (Decimal("1") + _LEG1_SENDMAX_BUFFER)
        )
        assert tx["SendMax"] == str(expected_drops)
        assert tx["SendMax"] == "5050000"

    def test_sendmax_is_string_not_dict(self, same_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        # SendMax must be XRP drops (string), never an IOU dict on leg 1
        assert isinstance(tx["SendMax"], str)

    def test_no_paths_field(self, same_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        assert "Paths" not in tx

    def test_no_flags_field(self, same_issuer_opp):
        # tfPartialPayment is forbidden on either leg per protocol+design
        tx = _build_leg1_tx(WALLET_ADDR, same_issuer_opp)
        assert "Flags" not in tx

    def test_cross_issuer_leg1_uses_buy_issuer(self, cross_issuer_opp):
        tx = _build_leg1_tx(WALLET_ADDR, cross_issuer_opp)
        # Leg 1 always targets the cheap (buy) side, regardless of cross-issuer
        assert tx["Amount"]["issuer"] == BUY_ISSUER

    def test_missing_iou_currency_raises(self):
        opp = Opportunity(
            input_xrp=Decimal("5"),
            output_xrp=Decimal("5.05"),
            profit_pct=Decimal("1.0"),
            profit_ratio=Decimal("0.01"),
            iou_currency="",
            buy_issuer=BUY_ISSUER,
            iou_amount=Decimal("2.5"),
        )
        with pytest.raises(ValueError, match="iou_currency"):
            _build_leg1_tx(WALLET_ADDR, opp)

    def test_zero_iou_amount_raises(self):
        opp = Opportunity(
            input_xrp=Decimal("5"),
            output_xrp=Decimal("5.05"),
            profit_pct=Decimal("1.0"),
            profit_ratio=Decimal("0.01"),
            iou_currency="USD",
            buy_issuer=BUY_ISSUER,
            iou_amount=Decimal("0"),
        )
        with pytest.raises(ValueError, match="iou_amount"):
            _build_leg1_tx(WALLET_ADDR, opp)


# ===========================================================================
# Helper: _build_leg2_tx
# ===========================================================================


class TestBuildLeg2Tx:
    def test_basic_shape(self, same_issuer_opp):
        tx = _build_leg2_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        assert tx["TransactionType"] == "Payment"
        assert tx["Account"] == WALLET_ADDR
        assert tx["Destination"] == WALLET_ADDR

    def test_amount_is_xrp_drops_string(self, same_issuer_opp):
        tx = _build_leg2_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        # output_xrp = 5.05 → 5_050_000 drops
        assert tx["Amount"] == "5050000"
        assert isinstance(tx["Amount"], str)

    def test_sendmax_uses_delivered_amount_not_theoretical(
        self, same_issuer_opp
    ):
        # Leg 1 actually delivered 2.4987 (less than theoretical 2.5)
        delivered = Decimal("2.4987")
        tx = _build_leg2_tx(WALLET_ADDR, same_issuer_opp, delivered)
        assert tx["SendMax"]["value"] == "2.4987"
        assert tx["SendMax"]["value"] != "2.5"

    def test_sendmax_sources_from_buy_issuer(self, cross_issuer_opp):
        # SendMax is the IOU we hold, which came from buy_issuer
        tx = _build_leg2_tx(WALLET_ADDR, cross_issuer_opp, Decimal("5.0"))
        assert tx["SendMax"]["issuer"] == BUY_ISSUER

    def test_same_issuer_has_no_paths(self, same_issuer_opp):
        tx = _build_leg2_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        assert "Paths" not in tx

    def test_cross_issuer_routes_through_sell_issuer(self, cross_issuer_opp):
        tx = _build_leg2_tx(WALLET_ADDR, cross_issuer_opp, Decimal("5.0"))
        assert "Paths" in tx
        # Exactly one path through the rich (sell) issuer's book
        assert len(tx["Paths"]) == 1
        assert len(tx["Paths"][0]) == 1
        step = tx["Paths"][0][0]
        assert step["currency"] == "USD"
        assert step["issuer"] == SELL_ISSUER

    def test_no_flags_field(self, cross_issuer_opp):
        tx = _build_leg2_tx(WALLET_ADDR, cross_issuer_opp, Decimal("5.0"))
        assert "Flags" not in tx

    def test_zero_iou_amount_raises(self, same_issuer_opp):
        with pytest.raises(ValueError, match="iou_amount_to_sell"):
            _build_leg2_tx(WALLET_ADDR, same_issuer_opp, Decimal("0"))


# ===========================================================================
# TradeExecutor: safety gates
# ===========================================================================


@pytest.mark.asyncio
async def test_circuit_breaker_halted_skips(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    mock_circuit_breaker.is_halted.return_value = True
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    result = await executor.execute(same_issuer_opp)
    assert result is False


@pytest.mark.asyncio
async def test_blacklisted_route_skips(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    mock_blacklist.is_blacklisted.return_value = True
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    result = await executor.execute(same_issuer_opp)
    assert result is False


@pytest.mark.asyncio
async def test_legacy_multihop_opp_is_skipped(
    multihop_legacy_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """Opps lacking two-leg metadata must be skipped, not crash.

    B4 removed multi-hop emission; this defends against any caller that
    hand-builds an Opportunity without iou_currency/buy_issuer.
    """
    # Use a real-shaped mock that doesn't claim every route_key is blocked
    mock_blacklist.is_route_blocked.return_value = False

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    # _fetch_account_info should never be reached — stub raises if called
    executor._fetch_account_info = AsyncMock(
        side_effect=AssertionError("should not be called for legacy opp")
    )
    result = await executor.execute(multihop_legacy_opp)
    assert result is False


@pytest.mark.asyncio
async def test_route_blocked_skips(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """A time-blacklisted route must be skipped before autofill runs."""
    mock_blacklist.is_route_blocked.return_value = True
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    executor._fetch_account_info = AsyncMock(
        side_effect=AssertionError("should not be called for blocked route")
    )
    result = await executor.execute(same_issuer_opp)
    assert result is False
    mock_blacklist.is_route_blocked.assert_called_with(same_issuer_opp.route_key())


@pytest.mark.asyncio
async def test_trade_exceeds_abs_cap_skips(
    mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """A trade whose input_xrp exceeds MAX_TRADE_XRP_ABS must be skipped
    regardless of percentage-based sizing."""
    oversized = Opportunity(
        input_xrp=Decimal("100"),  # 100 XRP > MAX_TRADE_XRP_ABS (5.0 default)
        output_xrp=Decimal("101"),
        profit_pct=Decimal("1.0"),
        profit_ratio=Decimal("0.01"),
        iou_currency="USD",
        buy_issuer=BUY_ISSUER,
        sell_issuer=BUY_ISSUER,
        iou_amount=Decimal("50"),
        paths=[],
    )
    mock_blacklist.is_route_blocked.return_value = False
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    executor._fetch_account_info = AsyncMock(
        side_effect=AssertionError("should not be reached")
    )
    result = await executor.execute(oversized)
    assert result is False


@pytest.mark.asyncio
async def test_balance_guard_trips_below_floor(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """If current balance is below MIN_BALANCE_GUARD_PCT of reference, skip."""
    mock_blacklist.is_route_blocked.return_value = False
    mock_circuit_breaker.reference_balance = Decimal("100")
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    executor._fetch_account_info = AsyncMock(
        side_effect=AssertionError("should not be reached")
    )
    # 85 / 100 = 0.85 which is under the 0.95 default floor
    result = await executor.execute(same_issuer_opp, current_balance=Decimal("85"))
    assert result is False


@pytest.mark.asyncio
async def test_balance_guard_not_tripped_when_balance_ok(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """Balance above floor lets the trade proceed past the guard."""
    mock_blacklist.is_route_blocked.return_value = False
    mock_circuit_breaker.reference_balance = Decimal("100")
    with patch("src.executor.log_trade", new_callable=AsyncMock), \
         patch("src.executor.send_alert", new_callable=AsyncMock), \
         patch("src.executor.simulate_transaction", new_callable=AsyncMock) as mock_sim:
        mock_sim.side_effect = [_leg1_sim_success("2.5"), _leg2_sim_success()]
        executor = TradeExecutor(
            wallet=mock_wallet,
            circuit_breaker=mock_circuit_breaker,
            blacklist=mock_blacklist,
            dry_run=True,
        )
        _patch_autofill(executor)
        # 98 / 100 = 0.98 >= 0.95 floor → trade proceeds
        result = await executor.execute(
            same_issuer_opp, current_balance=Decimal("98")
        )
        assert result is True


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_leg1_sim_failure_records_to_blacklist(
    mock_sim, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    """Sim failures feed the route-level failure counter for auto-blacklisting."""
    mock_blacklist.is_route_blocked.return_value = False
    mock_sim.return_value = SimResult(success=False, result_code="tecPATH_DRY")
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)
    await executor.execute(same_issuer_opp)
    mock_blacklist.record_sim_failure.assert_called_once_with(
        same_issuer_opp.route_key()
    )


# ===========================================================================
# TradeExecutor: DRY_RUN path
# ===========================================================================


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_dry_run_simulates_both_legs_and_logs(
    mock_sim, mock_alert, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    mock_sim.side_effect = [_leg1_sim_success("2.5"), _leg2_sim_success()]

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    result = await executor.execute(same_issuer_opp)

    assert result is True
    assert mock_sim.call_count == 2, "Both legs must be pre-simulated"
    mock_log.assert_called_once()
    trade_data = mock_log.call_args[0][0]
    assert trade_data["dry_run"] is True
    assert trade_data["leg1_sim_result"] == "tesSUCCESS"
    assert trade_data["leg2_sim_result"] == "tesSUCCESS"
    assert trade_data["iou_amount_delivered"] == "2.5"
    assert trade_data["route_key"] == same_issuer_opp.route_key()
    mock_alert.assert_called_once()


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_dry_run_leg2_built_with_delivered_amount(
    mock_sim, mock_alert, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    """The leg-2 tx passed to simulate MUST use leg-1's delivered value,
    not the theoretical opportunity.iou_amount."""
    # Leg 1 reports that only 2.4987 delivered (slightly less than theoretical 2.5)
    mock_sim.side_effect = [_leg1_sim_success("2.4987"), _leg2_sim_success()]

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    await executor.execute(same_issuer_opp)

    # Second call was leg 2 — inspect its tx_dict
    leg2_call_tx = mock_sim.call_args_list[1][0][0]
    assert leg2_call_tx["SendMax"]["value"] == "2.4987"
    # Sequence for leg 2 must be N+1
    assert leg2_call_tx["Sequence"] == 101  # _patch_autofill uses 100


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_dry_run_cross_issuer_leg2_has_paths(
    mock_sim, mock_alert, mock_log,
    cross_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    mock_sim.side_effect = [_leg1_sim_success("1.5"), _leg2_sim_success()]

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    await executor.execute(cross_issuer_opp)

    leg2_call_tx = mock_sim.call_args_list[1][0][0]
    assert "Paths" in leg2_call_tx
    assert leg2_call_tx["Paths"][0][0]["issuer"] == SELL_ISSUER


# ===========================================================================
# TradeExecutor: simulation gates
# ===========================================================================


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_leg1_sim_failure_aborts_before_leg2(
    mock_sim, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    mock_sim.return_value = SimResult(success=False, result_code="tecPATH_DRY")

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    result = await executor.execute(same_issuer_opp)

    assert result is False
    # Only leg 1 simulated; leg 2 never attempted
    assert mock_sim.call_count == 1
    mock_log.assert_not_called()


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_leg2_sim_failure_aborts_with_no_state(
    mock_sim, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    # Leg 1 passes, leg 2 fails
    mock_sim.side_effect = [
        _leg1_sim_success("2.5"),
        SimResult(success=False, result_code="tecPATH_PARTIAL"),
    ]

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    result = await executor.execute(same_issuer_opp)

    assert result is False
    assert mock_sim.call_count == 2
    mock_log.assert_not_called()


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_leg1_sim_missing_delivered_amount_skips(
    mock_sim, mock_log,
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    """A leg-1 sim that succeeds but omits delivered_amount can't
    parameterize leg 2 safely — must skip."""
    # tesSUCCESS but no meta.delivered_amount
    weird_leg1 = SimResult(
        success=True,
        result_code="tesSUCCESS",
        raw={"engine_result": "tesSUCCESS", "meta": {"TransactionResult": "tesSUCCESS"}},
    )
    mock_sim.return_value = weird_leg1

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    _patch_autofill(executor)

    result = await executor.execute(same_issuer_opp)
    assert result is False
    # Only leg 1 simulated (we bailed before leg 2)
    assert mock_sim.call_count == 1


@pytest.mark.asyncio
async def test_autofill_failure_skips_opportunity(
    same_issuer_opp, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    executor._fetch_account_info = AsyncMock(return_value=None)
    result = await executor.execute(same_issuer_opp)
    assert result is False


# ===========================================================================
# TradeExecutor: recovery stub
# ===========================================================================


# ===========================================================================
# Recovery flow (Phase C)
# ===========================================================================


def _ok_leg2_submit() -> dict:
    return {
        "success": True, "tx_hash": "RETRY_OK_HASH",
        "engine_result": "tesSUCCESS", "validated": True,
    }


def _fail_leg2_submit(code: str = "tecPATH_DRY") -> dict:
    return {
        "success": False, "tx_hash": "FAIL_HASH",
        "engine_result": code, "validated": True,
    }


@pytest.fixture
def recovery_executor(mock_circuit_breaker, mock_blacklist, mock_wallet):
    ex = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=False,
    )
    _patch_autofill(ex)
    return ex


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_recover_retry_first_attempt_succeeds(
    mock_alert, mock_log,
    same_issuer_opp, recovery_executor,
):
    """First leg-2 retry succeeds → record P&L, return False (execute path
    already aborted but state is clean and trade completed at spec profit)."""
    recovery_executor._submit_and_wait = AsyncMock(return_value=_ok_leg2_submit())

    trade_data = {"iou_amount_delivered": "2.5", "profit_pct": "1.0"}
    leg1_result = {"tx_hash": "LEG1", "engine_result": "tesSUCCESS", "validated": True}

    result = await recovery_executor._recover(
        same_issuer_opp, leg1_result, {"TransactionType": "Payment"},
        trade_data, reason="leg2_failed: tecPATH_DRY",
    )
    assert result is False  # always False from _recover
    logged = mock_log.call_args[0][0]
    assert logged["recovery_outcome"] == "leg2_retry_1"
    # Profit recorded on circuit breaker
    recovery_executor.circuit_breaker.record_trade.assert_called_once()


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_recover_retry_exhausted_falls_to_dump(
    mock_alert, mock_log,
    same_issuer_opp, recovery_executor,
):
    """All LEG2_RETRY_MAX retries fail → market-dump succeeds on first try."""
    # 2 retries fail, then first dump succeeds
    recovery_executor._submit_and_wait = AsyncMock(side_effect=[
        _fail_leg2_submit("tecPATH_PARTIAL"),
        _fail_leg2_submit("tecPATH_PARTIAL"),
        {"success": True, "tx_hash": "DUMP_OK", "engine_result": "tesSUCCESS", "validated": True},
    ])

    trade_data = {"iou_amount_delivered": "2.5", "profit_pct": "1.0"}
    leg1_result = {"tx_hash": "LEG1", "engine_result": "tesSUCCESS", "validated": True}

    await recovery_executor._recover(
        same_issuer_opp, leg1_result, {"TransactionType": "Payment"},
        trade_data, reason="leg2_failed",
    )
    logged = mock_log.call_args[0][0]
    assert logged["recovery_outcome"] == "dump_succeeded_attempt_1"
    # Loss recorded as negative (RECOVERY_MAX_LOSS_PCT * input_xrp)
    call_arg = recovery_executor.circuit_breaker.record_trade.call_args[0][0]
    assert call_arg < Decimal("0")


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_recover_all_fail_triggers_halt_and_blacklist(
    mock_alert, mock_log,
    same_issuer_opp, recovery_executor,
):
    """All retries + all dumps fail → halt_for + block_route + return False."""
    # 2 retries fail, 2 dumps fail
    recovery_executor._submit_and_wait = AsyncMock(return_value=_fail_leg2_submit())

    trade_data = {"iou_amount_delivered": "2.5", "profit_pct": "1.0"}
    leg1_result = {"tx_hash": "LEG1", "engine_result": "tesSUCCESS", "validated": True}

    result = await recovery_executor._recover(
        same_issuer_opp, leg1_result, {"TransactionType": "Payment"},
        trade_data, reason="leg2_failed",
    )
    assert result is False
    logged = mock_log.call_args[0][0]
    assert logged["recovery_outcome"] == "halt_and_blacklist"

    # Both safety mechanisms fired
    recovery_executor.circuit_breaker.halt_for.assert_called_once()
    halt_call = recovery_executor.circuit_breaker.halt_for.call_args
    assert halt_call.kwargs["hours"] > 0
    recovery_executor.blacklist.block_route.assert_called_once_with(
        same_issuer_opp.route_key()
    )


@pytest.mark.asyncio
async def test_recover_zero_iou_skips_dump_goes_straight_to_halt(
    same_issuer_opp, recovery_executor,
):
    """If iou_amount_delivered is 0 (nothing actually delivered), skip dump
    and escalate straight to halt."""
    recovery_executor._submit_and_wait = AsyncMock(return_value=_fail_leg2_submit())

    trade_data = {"iou_amount_delivered": "0", "profit_pct": "1.0"}
    leg1_result = {"tx_hash": "LEG1", "engine_result": "tesSUCCESS", "validated": True}

    with patch("src.executor.log_trade", new_callable=AsyncMock), \
         patch("src.executor.send_alert", new_callable=AsyncMock):
        await recovery_executor._recover(
            same_issuer_opp, leg1_result, {"TransactionType": "Payment"},
            trade_data, reason="leg2_failed",
        )
    recovery_executor.circuit_breaker.halt_for.assert_called_once()


# ---------------------------------------------------------------------------
# Market-dump + startup-drain builders
# ---------------------------------------------------------------------------


class TestBuildMarketDumpTx:
    def test_shape(self, same_issuer_opp):
        from src.executor import _build_market_dump_tx
        tx = _build_market_dump_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        assert tx["TransactionType"] == "Payment"
        assert tx["Account"] == WALLET_ADDR
        assert tx["Destination"] == WALLET_ADDR

    def test_amount_is_floor_in_drops(self, same_issuer_opp):
        """Amount = input_xrp * (1 - max_loss_pct) * DROPS_PER_XRP."""
        from src.executor import _build_market_dump_tx
        from src.config import RECOVERY_MAX_LOSS_PCT
        tx = _build_market_dump_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        expected = int(
            same_issuer_opp.input_xrp * (Decimal("1") - RECOVERY_MAX_LOSS_PCT)
            * Decimal("1000000")
        )
        assert tx["Amount"] == str(expected)

    def test_sendmax_is_iou_held(self, same_issuer_opp):
        from src.executor import _build_market_dump_tx
        tx = _build_market_dump_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        assert tx["SendMax"]["value"] == "2.5"
        assert tx["SendMax"]["issuer"] == BUY_ISSUER

    def test_no_partial_payment_flag(self, same_issuer_opp):
        """Atomic floor requires non-partial semantics."""
        from src.executor import _build_market_dump_tx
        tx = _build_market_dump_tx(WALLET_ADDR, same_issuer_opp, Decimal("2.5"))
        assert "Flags" not in tx

    def test_cross_issuer_routes_paths(self, cross_issuer_opp):
        from src.executor import _build_market_dump_tx
        tx = _build_market_dump_tx(WALLET_ADDR, cross_issuer_opp, Decimal("1.5"))
        assert "Paths" in tx
        assert tx["Paths"][0][0]["issuer"] == SELL_ISSUER

    def test_zero_iou_held_raises(self, same_issuer_opp):
        from src.executor import _build_market_dump_tx
        with pytest.raises(ValueError):
            _build_market_dump_tx(WALLET_ADDR, same_issuer_opp, Decimal("0"))


class TestBuildStartupDrainTx:
    def test_shape_and_partial_flag(self):
        from src.executor import _build_startup_drain_tx, _TF_PARTIAL_PAYMENT
        tx = _build_startup_drain_tx(
            WALLET_ADDR, "USD", BUY_ISSUER, Decimal("3.14"),
        )
        assert tx["TransactionType"] == "Payment"
        assert tx["Flags"] == _TF_PARTIAL_PAYMENT
        assert tx["Amount"] == "1000000000"  # ceiling, partial semantics
        assert tx["SendMax"]["value"] == "3.14"
        assert tx["SendMax"]["currency"] == "USD"

    def test_zero_balance_raises(self):
        from src.executor import _build_startup_drain_tx
        with pytest.raises(ValueError):
            _build_startup_drain_tx(WALLET_ADDR, "USD", BUY_ISSUER, Decimal("0"))


# ---------------------------------------------------------------------------
# TradeExecutor.drain_iou (startup recovery)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_iou_success(recovery_executor):
    """drain_iou returns True on successful dump."""
    recovery_executor._submit_and_wait = AsyncMock(return_value={
        "success": True, "tx_hash": "DRAIN_OK",
        "engine_result": "tesSUCCESS", "validated": True,
    })
    ok = await recovery_executor.drain_iou("USD", BUY_ISSUER, Decimal("1.23"))
    assert ok is True


@pytest.mark.asyncio
async def test_drain_iou_autofill_failure_returns_false(recovery_executor):
    recovery_executor._fetch_account_info = AsyncMock(return_value=None)
    ok = await recovery_executor.drain_iou("USD", BUY_ISSUER, Decimal("1.23"))
    assert ok is False


@pytest.mark.asyncio
async def test_drain_iou_submit_failure_returns_false(recovery_executor):
    recovery_executor._submit_and_wait = AsyncMock(return_value={
        "success": False, "tx_hash": "DRAIN_FAIL",
        "engine_result": "tecPATH_DRY", "validated": True,
    })
    ok = await recovery_executor.drain_iou("USD", BUY_ISSUER, Decimal("1.23"))
    assert ok is False
