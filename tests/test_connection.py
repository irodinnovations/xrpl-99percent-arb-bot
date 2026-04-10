"""Tests for XRPL connection module."""

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from src.connection import XRPLConnection


def test_init_default_url():
    conn = XRPLConnection(ws_url="wss://test.example.com")
    assert conn.ws_url == "wss://test.example.com"
    assert conn.connected is False
    assert conn.ledger_index == 0


def test_reconnect_backoff():
    conn = XRPLConnection()
    assert conn._reconnect_delay == 1.0
    # Simulate backoff increments
    conn._reconnect_delay = min(conn._reconnect_delay * 2, conn._max_reconnect_delay)
    assert conn._reconnect_delay == 2.0
    conn._reconnect_delay = min(conn._reconnect_delay * 2, conn._max_reconnect_delay)
    assert conn._reconnect_delay == 4.0
    # Verify max cap
    conn._reconnect_delay = 32.0
    conn._reconnect_delay = min(conn._reconnect_delay * 2, conn._max_reconnect_delay)
    assert conn._reconnect_delay == 30.0


def test_on_ledger_close_registers_callback():
    conn = XRPLConnection()
    async def dummy(idx): pass
    conn.on_ledger_close(dummy)
    assert len(conn._on_ledger_callbacks) == 1


@pytest.mark.asyncio
async def test_get_account_balance_returns_decimal():
    conn = XRPLConnection()
    conn.client = AsyncMock()
    conn.connected = True

    mock_response = MagicMock()
    mock_response.is_successful.return_value = True
    mock_response.result = {
        "account_data": {"Balance": "100000000"}  # 100 XRP in drops
    }
    conn.client.request = AsyncMock(return_value=mock_response)

    balance = await conn.get_account_balance("rTestAddress")
    assert balance == Decimal("100")
    assert isinstance(balance, Decimal)
