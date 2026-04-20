"""Replay harness for the 2026-04-19 incident (4 failed live trades).

Each parameterized case reconstructs an Opportunity from fixture data and
asserts the atomic two-leg flow would have fired both submits without a
validation wait between them — proving the 5-7s drift window is eliminated.

Per 05-RESEARCH.md Open Question #2: we do NOT attempt to re-simulate
against live historical mainnet state because mainnet simulate RPC has no
ledger_index parameter. The architectural fix is about timing (atomic
submit), not about reproducing historical book state. This test proves
the timing fix at the code level.

Run only this suite: pytest -m replay tests/test_replay_incident.py
Skip this suite:     pytest -m "not replay"
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.executor import TradeExecutor
from src.pathfinder import Opportunity
# Direct import works because tests/__init__.py exists; pytest fixtures are also auto-injected.
from tests.conftest import account_info_response, simulate_response, submit_response


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "incident_2026_04_19" / "hashes.json"


def _load_incident_hashes():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data["hashes"], data["issuer"]


INCIDENT_HASHES, INCIDENT_ISSUER = _load_incident_hashes()


def _opportunity_from_fixture(fixture: dict, issuer: str) -> Opportunity:
    """Build an Opportunity from a fixture entry."""
    opp = fixture["opportunity"]
    return Opportunity(
        input_xrp=Decimal(opp["input_xrp"]),
        output_xrp=Decimal(opp["output_xrp"]),
        profit_pct=Decimal(opp["profit_pct"]),
        profit_ratio=Decimal(opp["profit_ratio"]),
        paths=[[
            {"currency": "USD", "issuer": issuer},
            {"currency": "XRP"},
        ]],
        source_currency="XRP",
    )


@pytest.mark.replay
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    INCIDENT_HASHES,
    ids=[h["hash"] for h in INCIDENT_HASHES],
)
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_replay_incident_atomic_passes_both_sim_gates(
    mock_alert, mock_log_leg, mock_log_summary,
    fixture,
    mock_wallet, mock_circuit_breaker, mock_blacklist, mock_ws_connection,
):
    """For each 2026-04-19 incident hash: atomic submit passes BOTH sim gates."""
    opportunity = _opportunity_from_fixture(fixture, INCIDENT_ISSUER)

    # Happy-path sim: both legs pass cleanly at the same pre-state snapshot
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS",
                              delivered_iou=str(opportunity.output_xrp)),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", f"LEG1_{fixture['hash']}"),
            submit_response("tesSUCCESS", f"LEG2_{fixture['hash']}"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(opportunity)
    assert result is True, (
        f"Replay for {fixture['hash']}: atomic flow rejected a valid opportunity "
        f"that should have passed both sim gates"
    )

    # Summary entry must be both_legs_success — not pre_submit_gate_failed
    success_calls = [
        c.kwargs for c in mock_log_summary.call_args_list
        if c.kwargs.get("outcome") == "both_legs_success"
    ]
    assert len(success_calls) == 1


@pytest.mark.replay
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    INCIDENT_HASHES,
    ids=[h["hash"] for h in INCIDENT_HASHES],
)
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_replay_incident_leg2_terPRE_SEQ_boundary(
    mock_alert, mock_log_leg, mock_log_summary,
    fixture,
    mock_wallet, mock_circuit_breaker, mock_blacklist, mock_ws_connection,
):
    """Replay variant: leg 2 sim returns terPRE_SEQ (state-dependent pass).

    This is the boundary case — at the pre-leg-1 state, leg 2's Sequence N+1
    is ahead of account Sequence N, so rippled may return terPRE_SEQ instead
    of tesSUCCESS. The atomic gate must accept this per ATOM-07.
    """
    opportunity = _opportunity_from_fixture(fixture, INCIDENT_ISSUER)

    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS",
                              delivered_iou=str(opportunity.output_xrp)),
            simulate_response("terPRE_SEQ"),  # state-dependent pass on leg 2
        ],
        "submit": [
            submit_response("tesSUCCESS", f"LEG1_{fixture['hash']}"),
            submit_response("tesSUCCESS", f"LEG2_{fixture['hash']}"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(opportunity)
    assert result is True, (
        f"Replay (terPRE_SEQ variant) for {fixture['hash']}: atomic flow "
        f"should accept leg-2 terPRE_SEQ as a pass (ATOM-07)"
    )


@pytest.mark.replay
@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_replay_incident_no_drift_window_between_legs(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist, mock_ws_connection,
):
    """Architectural proof: NO `tx` / `submit_and_wait` call between the two submits.

    This is the heart of the 2026-04-19 fix. The original sequential flow
    waited for leg 1 validation before building + submitting leg 2, creating
    a 5-7s drift window. Atomic flow submits back-to-back with no wait.
    """
    fixture = INCIDENT_HASHES[0]
    opportunity = _opportunity_from_fixture(fixture, INCIDENT_ISSUER)

    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS",
                              delivered_iou=str(opportunity.output_xrp)),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", f"LEG1_{fixture['hash']}"),
            submit_response("tesSUCCESS", f"LEG2_{fixture['hash']}"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    await executor.execute(opportunity)

    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    submits = [i for i, c in enumerate(commands) if c == "submit"]
    assert len(submits) == 2, f"Expected 2 submits, got {len(submits)}: {commands}"
    between = commands[submits[0] + 1:submits[1]]
    # THE CORE ARCHITECTURAL ASSERTION
    assert "tx" not in between, (
        f"Drift-window regression: `tx` lookup between submits — {between}"
    )
    assert "submit_and_wait" not in between, (
        f"Drift-window regression: `submit_and_wait` between submits — {between}"
    )
    # Nothing should separate them except possibly internal bookkeeping; ideally empty
    assert all(c not in ("tx", "submit_and_wait", "ledger") for c in between), (
        f"Unexpected commands between submits: {between}"
    )
