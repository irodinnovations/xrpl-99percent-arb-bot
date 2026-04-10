"""Tests for Telegram alerts — including graceful skip."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
@patch("src.telegram_alerts.TELEGRAM_TOKEN", "test-token")
@patch("src.telegram_alerts.TELEGRAM_CHAT_ID", "12345")
@patch("src.telegram_alerts.requests")
async def test_send_alert_calls_telegram_api(mock_requests):
    from src.telegram_alerts import send_alert
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_requests.post.return_value = mock_response

    await send_alert("Test alert message")

    mock_requests.post.assert_called_once()
    call_args = mock_requests.post.call_args
    assert "api.telegram.org" in call_args[0][0]
    assert call_args[1]["json"]["text"] == "Test alert message"
    assert call_args[1]["json"]["chat_id"] == "12345"


@pytest.mark.asyncio
@patch("src.telegram_alerts.TELEGRAM_TOKEN", "")
@patch("src.telegram_alerts.TELEGRAM_CHAT_ID", "12345")
@patch("src.telegram_alerts.requests")
async def test_send_alert_skips_when_no_token(mock_requests):
    from src.telegram_alerts import send_alert
    await send_alert("Should not send")
    mock_requests.post.assert_not_called()


@pytest.mark.asyncio
@patch("src.telegram_alerts.TELEGRAM_TOKEN", "test-token")
@patch("src.telegram_alerts.TELEGRAM_CHAT_ID", "")
@patch("src.telegram_alerts.requests")
async def test_send_alert_skips_when_no_chat_id(mock_requests):
    from src.telegram_alerts import send_alert
    await send_alert("Should not send")
    mock_requests.post.assert_not_called()


@pytest.mark.asyncio
@patch("src.telegram_alerts.TELEGRAM_TOKEN", "test-token")
@patch("src.telegram_alerts.TELEGRAM_CHAT_ID", "12345")
@patch("src.telegram_alerts.requests")
async def test_send_alert_handles_request_error(mock_requests):
    from src.telegram_alerts import send_alert
    import requests as real_requests
    mock_requests.post.side_effect = real_requests.RequestException("timeout")
    mock_requests.RequestException = real_requests.RequestException

    # Should not raise
    await send_alert("Should handle error")


@pytest.mark.asyncio
@patch("src.telegram_alerts.TELEGRAM_TOKEN", "test-token")
@patch("src.telegram_alerts.TELEGRAM_CHAT_ID", "12345")
@patch("src.telegram_alerts.requests")
async def test_send_alert_url_format(mock_requests):
    from src.telegram_alerts import send_alert
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_requests.post.return_value = mock_response

    await send_alert("URL test")

    call_url = mock_requests.post.call_args[0][0]
    assert call_url == "https://api.telegram.org/bottest-token/sendMessage"
