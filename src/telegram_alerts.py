"""Telegram alert module — sends trade notifications with graceful skip.

TELE-01: Sends alert on every opportunity detected (paper or live).
TELE-02: Alerts include profit percentage, input/output amounts, and trade mode.
TELE-03: Bot works without Telegram configured — graceful skip when token/chat_id empty.
"""

import asyncio
import logging

import requests

from src.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def send_alert(message: str) -> None:
    """Send a Telegram alert message. Gracefully skips if not configured (TELE-03).

    Does nothing (no error, no HTTP call) when TELEGRAM_TOKEN or TELEGRAM_CHAT_ID
    is empty or not set in .env.

    Uses asyncio.to_thread to run the blocking HTTP call without blocking the event loop.
    Timeout of 10s prevents blocking the bot on slow Telegram API responses (T-01-20).
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping alert")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        response = await asyncio.to_thread(
            requests.post, url, json=payload, timeout=10
        )
        if response.status_code == 200:
            logger.debug("Telegram alert sent successfully")
        else:
            logger.warning(
                f"Telegram API returned {response.status_code}: {response.text}"
            )
    except requests.RequestException as e:
        logger.warning(f"Telegram alert failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected Telegram error: {e}")
