"""Tests for TradeExecutor — DRY_RUN branching and safety gates."""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from src.executor import TradeExecutor
from src.pathfinder import Opportunity
from src.simulator import SimResult


@pytest.fixture
def mock_opportunity():
    return Opportunity(
        input_xrp=Decimal("5"),
        output_xrp=Decimal("5.05"),
        profit_pct=Decimal("0.7"),
        profit_ratio=Decimal("0.007"),
        paths=[],
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
    return w


@pytest.mark.asyncio
@patch("src.executor.log_trade", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_dry_run_logs_without_submit(
    mock_sim, mock_alert, mock_log, mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    mock_sim.return_value = SimResult(success=True, result_code="tesSUCCESS")

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    result = await executor.execute(mock_opportunity)

    assert result is True
    mock_log.assert_called_once()
    log_data = mock_log.call_args[0][0]
    assert log_data["dry_run"] is True
    mock_alert.assert_called_once()


@pytest.mark.asyncio
@patch("src.executor.simulate_transaction", new_callable=AsyncMock)
async def test_simulation_failure_skips(
    mock_sim, mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    mock_sim.return_value = SimResult(success=False, result_code="tecPATH_DRY")

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        dry_run=True,
    )
    result = await executor.execute(mock_opportunity)

    assert result is False


@pytest.mark.asyncio
async def test_circuit_breaker_halted_skips(
    mock_opportunity, mock_circuit_breaker, mock_blacklist, mock_wallet
):
    mock_circuit_breaker.is_halted.return_value = True

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
    )
    result = await executor.execute(mock_opportunity)

    assert result is False
