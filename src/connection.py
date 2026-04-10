"""XRPL WebSocket connection with auto-reconnect and ledger-close subscription."""

import asyncio
import logging
from decimal import Decimal
from typing import Optional, Callable, Awaitable

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Subscribe, AccountInfo

from src.config import XRPL_WS_URL, LOG_LEVEL

logger = logging.getLogger(__name__)


class XRPLConnection:
    """Manages persistent WebSocket connection to XRPL with auto-reconnect."""

    def __init__(self, ws_url: str = XRPL_WS_URL):
        self.ws_url = ws_url
        self.client: Optional[AsyncWebsocketClient] = None
        self.ledger_index: int = 0
        self.connected: bool = False
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 30.0
        self._on_ledger_callbacks: list[Callable[[int], Awaitable[None]]] = []

    def on_ledger_close(self, callback: Callable[[int], Awaitable[None]]):
        """Register a callback to be called on each ledger close."""
        self._on_ledger_callbacks.append(callback)

    async def connect(self):
        """Connect to XRPL WebSocket with auto-reconnect loop."""
        while True:
            try:
                async with AsyncWebsocketClient(self.ws_url) as client:
                    self.client = client
                    self.connected = True
                    self._reconnect_delay = 1.0  # Reset on successful connect
                    logger.info(f"Connected to XRPL at {self.ws_url}")

                    # Subscribe to ledger close events
                    subscribe = Subscribe(streams=["ledger"])
                    await client.send(subscribe)
                    logger.info("Subscribed to ledger close stream")

                    # Listen for messages
                    async for message in client:
                        if isinstance(message, dict) and message.get("type") == "ledgerClosed":
                            self.ledger_index = message.get("ledger_index", 0)
                            logger.debug(f"Ledger closed: {self.ledger_index}")
                            for cb in self._on_ledger_callbacks:
                                try:
                                    await cb(self.ledger_index)
                                except Exception as e:
                                    logger.error(f"Ledger callback error: {e}")

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
        """Send a request through the WebSocket client. Returns response result or None."""
        if not self.client or not self.connected:
            logger.error("Cannot send request — not connected")
            return None
        try:
            response = await self.client.request(request)
            if response.is_successful():
                return response.result
            else:
                logger.error(f"Request failed: {response.result}")
                return None
        except Exception as e:
            logger.error(f"Request error: {e}")
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
