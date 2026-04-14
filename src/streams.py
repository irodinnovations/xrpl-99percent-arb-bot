"""Extended XRPL stream types and raw WebSocket helpers.

xrpl-py's StreamParameter enum does not include BOOK_CHANGES (as of v3.x).
This module provides:

1. ExtendedStreamParameter — a project-level enum with all standard streams
   plus BOOK_CHANGES, giving full type safety in our codebase.

2. subscribe_streams() — subscribes to any combination of standard and
   extended streams via the appropriate mechanism (xrpl-py model for standard
   streams, raw WebSocket JSON for extended ones).

3. send_raw_request() — sends an arbitrary dict as a WebSocket JSON-RPC
   request with proper Future-based response matching, following the same
   pattern as xrpl-py's internal WebsocketBase._do_request_impl but bypassing
   model validation.  Used for WebSocket-based simulate RPC.

Internal implementation accesses xrpl-py's _websocket (a standard
websockets.asyncio.client.ClientConnection) and _open_requests (a dict of
asyncio.Future keyed by request ID).  These are stable internals across
xrpl-py 3.x — pin the version in requirements.txt.
"""

import asyncio
import json
import logging
from enum import Enum
from random import randrange
from typing import Any, Optional

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Subscribe
from xrpl.models.requests.subscribe import StreamParameter, SubscribeBook

logger = logging.getLogger(__name__)

_REQ_ID_MAX = 1_000_000
_RAW_REQUEST_TIMEOUT = 30.0


class ExtendedStreamParameter(str, Enum):
    """All XRPL subscription streams including those missing from xrpl-py.

    Values match the exact strings the rippled server expects.
    Standard streams are duplicated from xrpl-py's StreamParameter so that
    callers can use a single enum for all stream types.
    """

    # Standard streams (present in xrpl-py's StreamParameter)
    CONSENSUS = "consensus"
    LEDGER = "ledger"
    MANIFESTS = "manifests"
    PEER_STATUS = "peer_status"
    TRANSACTIONS = "transactions"
    TRANSACTIONS_PROPOSED = "transactions_proposed"
    SERVER = "server"
    VALIDATIONS = "validations"

    # Extended streams (NOT in xrpl-py's enum — require raw WebSocket send)
    BOOK_CHANGES = "book_changes"


# Set of stream values that xrpl-py's StreamParameter natively supports.
_NATIVE_STREAMS = {sp.value for sp in StreamParameter}


async def subscribe_streams(
    client: AsyncWebsocketClient,
    streams: list[ExtendedStreamParameter],
    books: Optional[list[SubscribeBook]] = None,
) -> None:
    """Subscribe to XRPL streams, handling both native and extended types.

    Splits the requested streams into two groups:
    - Native streams (in xrpl-py's StreamParameter): sent via Subscribe model
    - Extended streams (like BOOK_CHANGES): sent as raw JSON over WebSocket

    Books subscriptions always use the xrpl-py Subscribe model.

    Args:
        client: An open AsyncWebsocketClient.
        streams: List of ExtendedStreamParameter values to subscribe to.
        books: Optional list of SubscribeBook objects for order book subs.
    """
    native = [StreamParameter(s.value) for s in streams if s.value in _NATIVE_STREAMS]
    extended = [s.value for s in streams if s.value not in _NATIVE_STREAMS]

    # Subscribe to native streams + books via xrpl-py model
    if native or books:
        sub = Subscribe(
            streams=native if native else None,
            books=books if books else None,
        )
        await client.send(sub)
        if native:
            logger.info(f"Subscribed (native): {[s.value for s in native]}")
        if books:
            logger.info(f"Subscribed to {len(books)} order book(s)")

    # Subscribe to extended streams via raw WebSocket
    if extended:
        payload = json.dumps({
            "command": "subscribe",
            "streams": extended,
            "id": f"ext_subscribe_{randrange(_REQ_ID_MAX)}",
        })
        await client._websocket.send(payload)
        logger.info(f"Subscribed (extended): {extended}")


async def send_raw_request(
    client: AsyncWebsocketClient,
    payload: dict[str, Any],
    timeout: float = _RAW_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """Send a raw dict as a WebSocket JSON-RPC request and await the response.

    Follows the same pattern as xrpl-py's WebsocketBase._do_request_impl:
    1. Inject a unique request ID
    2. Register a Future in client._open_requests keyed by that ID
    3. Send the JSON over the raw websocket
    4. The client's internal _handler coroutine resolves the Future when
       a response with matching ID arrives
    5. Return the parsed response dict

    This bypasses xrpl-py's Request model validation, allowing us to send
    commands like 'simulate' that aren't modeled in xrpl-py.

    Args:
        client: An open AsyncWebsocketClient.
        payload: Raw request dict (must not already have 'id' key set).
        timeout: Seconds to wait for response before raising TimeoutError.

    Returns:
        The parsed JSON response dict from the server.

    Raises:
        TimeoutError: If the server doesn't respond within timeout.
        RuntimeError: If the client is not connected.
    """
    if not client.is_open():
        raise RuntimeError("WebSocket client is not open")

    # Inject a unique ID for response matching
    req_id = f"raw_{payload.get('command', 'req')}_{randrange(_REQ_ID_MAX)}"
    payload_with_id = {**payload, "id": req_id}

    # Register a Future for this request ID (same mechanism as xrpl-py)
    if req_id in client._open_requests and not client._open_requests[req_id].done():
        raise RuntimeError(f"Request {req_id} already in progress")
    client._open_requests[req_id] = asyncio.get_running_loop().create_future()

    # Send raw JSON
    await client._websocket.send(json.dumps(payload_with_id))

    try:
        response = await asyncio.wait_for(
            client._open_requests[req_id], timeout
        )
    finally:
        # Clean up the Future
        client._open_requests.pop(req_id, None)

    return response
