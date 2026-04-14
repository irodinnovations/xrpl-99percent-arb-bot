"""XRPL WebSocket connection with auto-reconnect and multi-stream subscriptions.

Subscribes to three streams on connect:
  - ledger: triggers scan cycles on each ledger close
  - transactions: feeds AMM event detection
  - book_changes: feeds volatility tracking

Message dispatch routes incoming messages to registered callbacks by type.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Any, Optional, Callable, Awaitable

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import AccountInfo

from src.config import XRPL_WS_URL
from src.streams import (
    ExtendedStreamParameter,
    subscribe_streams,
    send_raw_request,
)

logger = logging.getLogger(__name__)


class XRPLConnection:
    """Manages persistent WebSocket connection to XRPL with auto-reconnect.

    Supports three callback types:
      on_ledger_close(ledger_index: int)  — called on each ledger close
      on_transaction(msg: dict)           — called on each validated transaction
      on_book_changes(msg: dict)          — called on each book_changes summary
    """

    # Maximum concurrent RPC requests to the XRPL node.  Public nodes
    # return 'slowDown' (error 10) at ~10-15 concurrent requests.
    # This semaphore is shared by ALL callers (send_request + send_raw)
    # so the limit is enforced globally, not per-scan.
    MAX_CONCURRENT_REQUESTS = 3

    def __init__(self, ws_url: str = XRPL_WS_URL):
        self.ws_url = ws_url
        self.client: Optional[AsyncWebsocketClient] = None
        self.ledger_index: int = 0
        self.connected: bool = False
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 30.0
        self._rpc_semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)
        self._on_ledger_callbacks: list[Callable[[int], Awaitable[None]]] = []
        self._on_transaction_callbacks: list[Callable[[dict], Awaitable[None]]] = []
        self._on_book_changes_callbacks: list[Callable[[dict], Awaitable[None]]] = []

    def on_ledger_close(self, callback: Callable[[int], Awaitable[None]]):
        """Register a callback to be called on each ledger close."""
        self._on_ledger_callbacks.append(callback)

    def on_transaction(self, callback: Callable[[dict], Awaitable[None]]):
        """Register a callback for validated transaction messages."""
        self._on_transaction_callbacks.append(callback)

    def on_book_changes(self, callback: Callable[[dict], Awaitable[None]]):
        """Register a callback for book_changes summaries (every ledger)."""
        self._on_book_changes_callbacks.append(callback)

    async def connect(self):
        """Connect to XRPL WebSocket with auto-reconnect loop.

        Subscribes to ledger, transactions, and book_changes streams.
        Routes incoming messages to registered callbacks by type.
        """
        while True:
            try:
                async with AsyncWebsocketClient(self.ws_url) as client:
                    self.client = client
                    self.connected = True
                    self._reconnect_delay = 1.0  # Reset on successful connect
                    logger.info(f"Connected to XRPL at {self.ws_url}")

                    # Subscribe to all streams
                    await subscribe_streams(
                        client,
                        streams=[
                            ExtendedStreamParameter.LEDGER,
                            ExtendedStreamParameter.TRANSACTIONS,
                            ExtendedStreamParameter.BOOK_CHANGES,
                        ],
                    )

                    # Listen for messages and dispatch to callbacks
                    async for message in client:
                        if not isinstance(message, dict):
                            continue

                        msg_type = message.get("type")

                        # Ledger close events
                        if msg_type == "ledgerClosed":
                            self.ledger_index = message.get("ledger_index", 0)
                            logger.debug(f"Ledger closed: {self.ledger_index}")
                            for cb in self._on_ledger_callbacks:
                                try:
                                    await cb(self.ledger_index)
                                except Exception as e:
                                    logger.error(f"Ledger callback error: {e}")

                        # Validated transaction events
                        elif msg_type == "transaction":
                            for cb in self._on_transaction_callbacks:
                                try:
                                    await cb(message)
                                except Exception as e:
                                    logger.error(f"Transaction callback error: {e}")

                        # book_changes summaries (sent every ledger close)
                        if "changes" in message and message.get("type") == "bookChanges":
                            for cb in self._on_book_changes_callbacks:
                                try:
                                    await cb(message)
                                except Exception as e:
                                    logger.error(f"Book changes callback error: {e}")

            except Exception as e:
                self.connected = False
                logger.warning(
                    f"Connection lost: {e}. Reconnecting in {self._reconnect_delay}s..."
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def send_request(self, request) -> Optional[dict]:
        """Send a request through the WebSocket client. Returns response result or None.

        Rate-limited by the connection-level semaphore to prevent
        overwhelming public XRPL nodes with concurrent requests.
        """
        if not self.client or not self.connected:
            logger.error("Cannot send request — not connected")
            return None
        try:
            async with self._rpc_semaphore:
                response = await self.client.request(request)
            if response.is_successful():
                return response.result
            else:
                logger.error(f"Request failed: {response.result}")
                return None
        except Exception as e:
            logger.error(f"Request error: {e}")
            return None

    async def send_raw(self, payload: dict[str, Any]) -> Optional[dict]:
        """Send a raw dict as a WebSocket JSON-RPC request and await response.

        Bypasses xrpl-py model validation, allowing commands like 'simulate'
        that aren't modeled in xrpl-py.  Rate-limited by the same semaphore
        as send_request.

        Returns the parsed response dict, or None on error.
        """
        if not self.client or not self.connected:
            logger.error("Cannot send raw request — not connected")
            return None
        try:
            async with self._rpc_semaphore:
                return await send_raw_request(self.client, payload)
        except TimeoutError:
            logger.error(f"Raw request timed out: {payload.get('command', '?')}")
            return None
        except Exception as e:
            logger.error(f"Raw request error: {e}")
            return None

    async def get_account_balance(self, account: str) -> Decimal:
        """Fetch account XRP balance as Decimal (in XRP, not drops)."""
        request = AccountInfo(account=account)
        result = await self.send_request(request)
        if result and "account_data" in result:
            drops = result["account_data"].get("Balance", "0")
            return Decimal(drops) / Decimal("1000000")
        logger.error(f"Failed to fetch balance for {account}")
        return Decimal("0")
