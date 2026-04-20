"""Shared pytest fixtures for the atomic-executor test suite.

These fixtures are consumed by tests/test_atomic_executor.py and
tests/test_replay_incident.py (Plan 05-05). They do not affect the
existing 194 tests since pytest fixtures are opt-in by argument name.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pathfinder import Opportunity


@pytest.fixture
def mock_wallet():
    """Wallet with enough attributes for signing path in TradeExecutor."""
    w = MagicMock()
    w.address = "r3yPcfPJuPkG1AJxNxbUpQHZVfEaa8VPKq"
    # 66-char hex strings — parseable by bytes.fromhex
    w.public_key = "ED" + "00" * 32
    w.private_key = "ED" + "11" * 32
    return w


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
def atomic_opportunity():
    """Realistic Opportunity with non-empty paths so _extract_intermediate succeeds."""
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
def mock_ws_connection():
    """WS connection whose send_raw dispatches to a per-command table.

    Tests set `conn.responses[command_name] = [dict, dict, ...]` — each send_raw
    call pops the next response for that command name (FIFO).
    """
    conn = MagicMock()
    conn.connected = True
    conn.responses = {}
    conn.send_raw_call_log = []

    async def _send_raw(payload):
        conn.send_raw_call_log.append(payload)
        cmd = payload.get("command")
        queue = conn.responses.get(cmd) or []
        if not queue:
            return None
        return queue.pop(0)

    conn.send_raw = AsyncMock(side_effect=_send_raw)
    return conn


def account_info_response(sequence: int, ledger: int):
    """Build a canonical account_info response for mock_ws_connection.responses."""
    return {
        "result": {
            "account_data": {"Sequence": sequence},
            "ledger_current_index": ledger,
        }
    }


def simulate_response(engine_result: str, delivered_iou: str | None = None):
    """Build a canonical simulate response.

    If delivered_iou is provided, includes meta.delivered_amount as an IOU dict
    so the executor's _extract_sim_delivered picks it up for leg-2 SendMax.
    """
    result = {
        "applied": engine_result == "tesSUCCESS",
        "engine_result": engine_result,
        "engine_result_code": 0 if engine_result == "tesSUCCESS" else -1,
    }
    if delivered_iou is not None:
        result["meta"] = {
            "TransactionResult": engine_result,
            "delivered_amount": {
                "currency": "USD",
                "issuer": "rhub8VRN55s94qWKDv6jmDy1pUykJzF3wq",
                "value": delivered_iou,
            },
        }
    return {"result": result}


def submit_response(engine_result: str, tx_hash: str):
    """Build a canonical submit response."""
    return {
        "result": {
            "engine_result": engine_result,
            "engine_result_code": 0 if engine_result == "tesSUCCESS" else -1,
            "tx_json": {"hash": tx_hash},
        }
    }


@pytest.fixture
def sim_factory():
    """Expose the builders to tests without requiring module-level imports."""
    return {
        "account_info": account_info_response,
        "simulate": simulate_response,
        "submit": submit_response,
    }
