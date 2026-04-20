"""Atomic two-leg submit — happy path and simulate gate tests (ATOM-01 to ATOM-03, ATOM-07).

ATOM-01 is split into TWO narrow tests per plan-checker Warning 3:
  - test_both_legs_simulated_before_first_submit  — proves simulate ordering
  - test_both_legs_signed_before_first_submit     — proves signing ordering
Each test's name reflects exactly what it asserts.
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.executor import TradeExecutor
from src.simulator import SimResult
# Direct import works because tests/__init__.py exists; pytest fixtures are also auto-injected.
from tests.conftest import account_info_response, simulate_response, submit_response


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_both_legs_simulated_before_first_submit(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-01 (simulate-ordering half): both simulate calls happen BEFORE any submit.

    Does NOT assert signing order — see test_both_legs_signed_before_first_submit
    for that half of ATOM-01.
    """
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),  # single-writer re-check
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is True

    # Assert ordering in send_raw_call_log: simulate, simulate, [account_info recheck], submit, submit
    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    first_submit = commands.index("submit")
    # Both simulate calls happened BEFORE the first submit
    assert commands[:first_submit].count("simulate") == 2


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_both_legs_signed_before_first_submit(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-01 (signing-ordering half): both `_sign_leg` signing primitives fire BEFORE any submit.

    This catches a future refactor that could interleave signing with submits
    (e.g., move leg-2 signing between the two submits, reintroducing a drift
    window). We spy on the module-level `keypairs_sign` function used by
    `TradeExecutor._sign_leg` and interleave a call-order witness with the
    mock WS `send_raw` call log.
    """
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    # Build a shared witness timeline so we can assert signing fires before submits
    event_log: list[str] = []

    # Wrap the WS send_raw to append its command to the shared timeline.
    original_send_raw = mock_ws_connection.send_raw.side_effect

    async def _tracked_send_raw(payload):
        event_log.append(f"send_raw:{payload.get('command')}")
        return await original_send_raw(payload)

    mock_ws_connection.send_raw = AsyncMock(side_effect=_tracked_send_raw)

    # Spy on the module-level signing primitive used inside _sign_leg.
    # Returns a dummy 128-char hex signature; tx_dict mutation still works.
    def _spy_sign(message_bytes, private_key_hex):
        event_log.append("keypairs_sign")
        return "00" * 64

    with patch("src.executor.keypairs_sign", side_effect=_spy_sign) as mock_sign:
        executor = TradeExecutor(
            wallet=mock_wallet,
            circuit_breaker=mock_circuit_breaker,
            blacklist=mock_blacklist,
            connection=mock_ws_connection,
            dry_run=False,
        )
        result = await executor.execute(atomic_opportunity)
        assert result is True

    # Signing must have happened at least twice (leg 1 + leg 2)
    sign_events = [i for i, e in enumerate(event_log) if e == "keypairs_sign"]
    submit_events = [
        i for i, e in enumerate(event_log) if e == "send_raw:submit"
    ]
    assert len(sign_events) >= 2, (
        f"Expected at least 2 keypairs_sign calls; saw {len(sign_events)} in {event_log}"
    )
    assert len(submit_events) >= 1, (
        f"Expected at least 1 submit call; saw {len(submit_events)} in {event_log}"
    )
    # CORE ASSERTION: BOTH leg signings complete strictly before the first submit
    first_submit_idx = submit_events[0]
    signs_before_first_submit = [i for i in sign_events if i < first_submit_idx]
    assert len(signs_before_first_submit) >= 2, (
        f"ATOM-01 signing-ordering violation: only {len(signs_before_first_submit)} "
        f"signing call(s) happened before the first submit. Timeline: {event_log}"
    )


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_atomic_sequences_are_n_and_n_plus_1(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-02: leg 1 uses Sequence N, leg 2 uses N+1 (from ONE account_info)."""
    N = 12345
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=N, ledger=99000000),
            account_info_response(sequence=N, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    await executor.execute(atomic_opportunity)

    # Extract the two submit tx_blobs and decode their Sequence
    from xrpl.core.binarycodec import decode as xrpl_decode
    submit_calls = [c for c in mock_ws_connection.send_raw_call_log if c["command"] == "submit"]
    assert len(submit_calls) == 2
    leg1_decoded = xrpl_decode(submit_calls[0]["tx_blob"])
    leg2_decoded = xrpl_decode(submit_calls[1]["tx_blob"])
    assert leg1_decoded["Sequence"] == N
    assert leg2_decoded["Sequence"] == N + 1
    # Both legs share the SAME LastLedgerSequence (Pitfall 2 mitigation)
    assert leg1_decoded["LastLedgerSequence"] == leg2_decoded["LastLedgerSequence"]


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_atomic_leg2_submits_before_leg1_validates(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-03: no `tx` or wait-for-validation call between the two submits."""
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    await executor.execute(atomic_opportunity)

    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    # Between the two submits, NO `tx` (validation lookup) or `submit_and_wait`
    first_submit = commands.index("submit")
    second_submit = commands.index("submit", first_submit + 1)
    between = commands[first_submit + 1:second_submit]
    assert "tx" not in between
    assert "submit_and_wait" not in between


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
async def test_leg1_sim_rejection_aborts_before_submit(
    mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """Sim gate: leg-1 sim failure prevents any submit."""
    mock_ws_connection.responses = {
        "account_info": [account_info_response(sequence=100, ledger=99000000)],
        "simulate": [simulate_response("tecPATH_DRY")],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    assert "submit" not in commands
    # Summary entry written with pre_submit_gate_failed
    assert mock_log_summary.call_args.kwargs["outcome"] == "pre_submit_gate_failed"


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
async def test_leg2_sim_rejection_aborts_before_submit(
    mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """Sim gate: leg-2 terminal-failure sim (not terPRE_SEQ) prevents submit."""
    mock_ws_connection.responses = {
        "account_info": [account_info_response(sequence=100, ledger=99000000)],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tecPATH_PARTIAL"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    assert "submit" not in commands


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_leg2_terPRE_SEQ_treated_as_pass(
    mock_alert, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-07 end-to-end: leg 2 sim returns terPRE_SEQ → executor proceeds.

    Uses DRY_RUN=True so we don't need to mock submit — the successful
    pass-through of both sim gates leads to the dry_run_would_execute branch.
    """
    mock_ws_connection.responses = {
        "account_info": [account_info_response(sequence=100, ledger=99000000)],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("terPRE_SEQ"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=True,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is True
    assert mock_log_summary.call_args.kwargs["outcome"] == "dry_run_would_execute"


# ========================================
# Failure path + recovery + Decimal tests
# ========================================

from decimal import Decimal
from src.executor import _is_terminal_failure, _extract_intermediate
from src.pathfinder import Opportunity


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_leg1_terminal_fail_burns_sequence(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-04: leg-1 tecPATH_PARTIAL → AccountSet burn at Sequence N+1."""
    N = 100
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=N, ledger=99000000),
            account_info_response(sequence=N, ledger=99000000),  # single-writer recheck
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tecPATH_PARTIAL", "LEG1HASH"),  # leg 1 terminal fail
            submit_response("tesSUCCESS", "BURNHASH"),       # burn succeeds
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    # Decode the two submits: first is leg 1 Payment, second is AccountSet burn at Seq N+1
    from xrpl.core.binarycodec import decode as xrpl_decode
    submits = [c for c in mock_ws_connection.send_raw_call_log if c["command"] == "submit"]
    assert len(submits) == 2
    burn_decoded = xrpl_decode(submits[1]["tx_blob"])
    assert burn_decoded["TransactionType"] == "AccountSet"
    assert burn_decoded["Sequence"] == N + 1

    # Summary logged with outcome leg1_fail_burned
    outcome_kwargs = [
        call.kwargs for call in mock_log_summary.call_args_list
        if call.kwargs.get("outcome") == "leg1_fail_burned"
    ]
    assert len(outcome_kwargs) == 1


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_leg1_terminal_fail_burn_also_fails_reports_escalation(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-04 edge: leg 1 tec AND burn submit fails (non-tesSUCCESS)."""
    N = 100
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=N, ledger=99000000),
            account_info_response(sequence=N, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tecPATH_PARTIAL", "LEG1HASH"),
            submit_response("terRETRY", "BURNHASH"),  # burn didn't commit
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    outcome_kwargs = [
        call.kwargs for call in mock_log_summary.call_args_list
        if call.kwargs.get("outcome") == "leg1_fail_burn_failed"
    ]
    assert len(outcome_kwargs) == 1


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_leg2_fail_activates_existing_recovery(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-05: leg-1 committed, leg-2 failed → CircuitBreaker.record_trade called with negative profit."""
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tecPATH_PARTIAL", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    # CircuitBreaker.record_trade MUST have been called with a NEGATIVE Decimal
    assert mock_circuit_breaker.record_trade.called
    profit_arg = mock_circuit_breaker.record_trade.call_args.args[0]
    assert isinstance(profit_arg, Decimal)
    assert profit_arg < Decimal("0")

    outcome_kwargs = [
        call.kwargs for call in mock_log_summary.call_args_list
        if call.kwargs.get("outcome") == "leg2_fail_recovery_activated"
    ]
    assert len(outcome_kwargs) == 1


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
async def test_single_writer_guard_rejects_concurrent(
    mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-06: second account_info shows Sequence drift → abort without submit."""
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=105, ledger=99000000),  # <-- drift
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    result = await executor.execute(atomic_opportunity)
    assert result is False

    commands = [c["command"] for c in mock_ws_connection.send_raw_call_log]
    assert "submit" not in commands

    outcome_kwargs = [
        call.kwargs for call in mock_log_summary.call_args_list
        if call.kwargs.get("outcome") == "single_writer_violation"
    ]
    assert len(outcome_kwargs) == 1


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_atomic_all_amounts_are_decimal(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-08: no float values anywhere in the signed tx dicts."""
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    await executor.execute(atomic_opportunity)

    from xrpl.core.binarycodec import decode as xrpl_decode
    submits = [c for c in mock_ws_connection.send_raw_call_log if c["command"] == "submit"]
    for s in submits:
        decoded = xrpl_decode(s["tx_blob"])
        # Walk all values — none should be a float
        def _has_no_float(node):
            if isinstance(node, dict):
                return all(_has_no_float(v) for v in node.values())
            if isinstance(node, list):
                return all(_has_no_float(v) for v in node)
            # Accept int, str, bool; reject float
            return not isinstance(node, float)
        assert _has_no_float(decoded), f"Float found in tx_blob: {decoded!r}"


@pytest.mark.asyncio
@patch("src.executor.log_trade_summary", new_callable=AsyncMock)
@patch("src.executor.log_trade_leg", new_callable=AsyncMock)
@patch("src.executor.send_alert", new_callable=AsyncMock)
async def test_atomic_per_leg_log_entries(
    mock_alert, mock_log_leg, mock_log_summary,
    mock_wallet, mock_circuit_breaker, mock_blacklist,
    atomic_opportunity, mock_ws_connection,
):
    """ATOM-09: log_trade_leg called twice — once per leg with all required fields."""
    mock_ws_connection.responses = {
        "account_info": [
            account_info_response(sequence=100, ledger=99000000),
            account_info_response(sequence=100, ledger=99000000),
        ],
        "simulate": [
            simulate_response("tesSUCCESS", delivered_iou="5.05"),
            simulate_response("tesSUCCESS"),
        ],
        "submit": [
            submit_response("tesSUCCESS", "LEG1HASH"),
            submit_response("tesSUCCESS", "LEG2HASH"),
        ],
    }

    executor = TradeExecutor(
        wallet=mock_wallet,
        circuit_breaker=mock_circuit_breaker,
        blacklist=mock_blacklist,
        connection=mock_ws_connection,
        dry_run=False,
    )
    await executor.execute(atomic_opportunity)

    assert mock_log_leg.call_count == 2
    # Verify leg numbers, hashes, and required fields
    calls = [c.kwargs for c in mock_log_leg.call_args_list]
    legs_seen = {c["leg"] for c in calls}
    assert legs_seen == {1, 2}
    for c in calls:
        assert "sequence" in c and isinstance(c["sequence"], int)
        assert "hash" in c
        assert "engine_result" in c
        assert "ledger_index" in c
        assert "dry_run" in c
    # Leg 2 has latency; leg 1 does not (latency_from_leg1_ms is None for leg 1)
    leg1_kwargs = next(c for c in calls if c["leg"] == 1)
    leg2_kwargs = next(c for c in calls if c["leg"] == 2)
    assert leg1_kwargs.get("latency_from_leg1_ms") is None
    assert leg2_kwargs.get("latency_from_leg1_ms") is not None
    assert isinstance(leg2_kwargs["latency_from_leg1_ms"], int)


# --- Pure unit tests for helpers ---

def test_terminal_failure_helper_classifies_correctly():
    assert _is_terminal_failure("tecPATH_PARTIAL") is True
    assert _is_terminal_failure("tefMAX_LEDGER") is True
    assert _is_terminal_failure("temBAD_AMOUNT") is True
    assert _is_terminal_failure("tesSUCCESS") is False
    assert _is_terminal_failure("terPRE_SEQ") is False
    assert _is_terminal_failure("telCAN_NOT_QUEUE") is False
    assert _is_terminal_failure("unknown") is False


def test_extract_intermediate_parses_opportunity_paths():
    opp = Opportunity(
        input_xrp=Decimal("1"),
        output_xrp=Decimal("1.01"),
        profit_pct=Decimal("1"),
        profit_ratio=Decimal("0.01"),
        paths=[[
            {"currency": "USD", "issuer": "rISSUER1"},
            {"currency": "XRP"},
        ]],
    )
    cur, iss = _extract_intermediate(opp)
    assert cur == "USD"
    assert iss == "rISSUER1"


def test_extract_intermediate_raises_on_xrp_only_paths():
    opp = Opportunity(
        input_xrp=Decimal("1"),
        output_xrp=Decimal("1.01"),
        profit_pct=Decimal("1"),
        profit_ratio=Decimal("0.01"),
        paths=[[{"currency": "XRP"}]],
    )
    with pytest.raises(ValueError):
        _extract_intermediate(opp)
