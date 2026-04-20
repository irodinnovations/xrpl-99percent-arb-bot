"""Tests for TradeExecutor — public contract (DRY_RUN branching and safety gates).

These three tests cover the stable PUBLIC interface only:
  - circuit breaker halt -> False
  - sim failure -> False
  - DRY_RUN True -> True (logs summary with outcome=dry_run_would_execute)

Internal atomic-architecture tests (ATOM-01 through ATOM-10) live in
tests/test_atomic_executor.py (Plan 05-04).
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from src.executor import TradeExecutor
from src.pathfinder import Opportunity
from src.simulator import SimResult


@pytest.fixture
def mock_opportunity():
    """Opportunity with a realistic non-empty path so _extract_intermediate succeeds."""
    return Opportunity(
        input_xrp=Decimal("5"),
        output_xrp=Decimal("5.05"),
        profit_pct=Decimal("0.7"),
        profit_ratio=Decimal("0.007"),
        paths=[[
            {"currency": "USD", "issuer": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq"},
            {"currency": "XRP"},
        ]],
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
    return bl


@pytest.fixture
def mock_wallet():
    w = MagicMock()
    w.address = "rTestAddress123"
    w.public_key = "ED" + "00" * 32  # 66-char hex string; only used in LIVE path
    w.private_key = "ED" + "00" * 32
    return w


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_dry_run_logs_without_submit(
    mock_sim, mock_alert, mock_log_leg, mock_log_summary,
    mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    """DRY_RUN=True path returns True and logs a summary with dry_run_would_execute."""
    # Both leg sims return tesSUCCESS so we reach the DRY_RUN branch
    mock_sim.return_value = SimResult(success=True, result_code="tesSUCCESS")

    # account_info response stub so _fetch_account_state succeeds
    mock_rpc = MagicMock()
    mock_rpc.request.return_value = {
        "result": {
            "account_data": {"Sequence": 100},
            "ledger_current_index": 99000000,
        }
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        rpc_client=mock_rpc,
        dry_run=True,
    )
    result = await executor.execute(mock_opportunity)

    assert result is True
    mock_log_summary.assert_called_once()
    kwargs = mock_log_summary.call_args.kwargs
    assert kwargs["outcome"] == "dry_run_would_execute"
    assert kwargs["dry_run"] is True


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_simulation_failure_skips(
    mock_sim, mock_log_summary,
    mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet,
):
    """Leg 1 sim failure returns False without submitting."""
    mock_sim.return_value = SimResult(success=False, result_code="tecPATH_DRY")

    mock_rpc = MagicMock()
    mock_rpc.request.return_value = {
        "result": {
            "account_data": {"Sequence": 100},
            "ledger_current_index": 99000000,
        }
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        rpc_client=mock_rpc,
        dry_run=True,
    )
    result = await executor.execute(mock_opportunity)

    assert result is False


@pytest.mark.asyncio
async def test_circuit_breaker_halted_skips(
    mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    """Circuit breaker halt is the first gate — returns False immediately."""
    mock_circuit_breaker.is_halted.return_value = True

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    result = await executor.execute(mock_opportunity)

    assert result is False
