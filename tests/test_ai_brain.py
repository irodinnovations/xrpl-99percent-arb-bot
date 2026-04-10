"""Unit tests for AI brain module (src/ai_brain.py).

Tests cover:
- AI-01: Async, non-blocking execution
- AI-02: Last 50 trades loaded as context
- AI-03: Structured JSON response parsed into AIReview dataclass
- AI-04: Graceful skip when ANTHROPIC_KEY is absent
"""

import asyncio
import json
import os
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, mock_open


# ---------------------------------------------------------------------------
# Test 1: review_trade() returns None immediately when ANTHROPIC_KEY is empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_trade_skips_when_no_api_key():
    """AI-04: Graceful skip when ANTHROPIC_KEY is not configured."""
    with patch("src.ai_brain.ANTHROPIC_KEY", ""):
        from src.ai_brain import review_trade
        trade_data = {
            "profit_pct": "0.8500",
            "profit_ratio": "0.0085",
            "input_xrp": "50.000000",
            "output_xrp": "50.425000",
            "dry_run": True,
        }
        result = await review_trade(trade_data)
        assert result is None


# ---------------------------------------------------------------------------
# Test 2: review_trade() calls Claude API with correct model and structured prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_trade_calls_correct_model():
    """Calls Claude API with model=claude-haiku-4-5 and max_tokens=500."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text='{"suggestion": "hold steady", "new_threshold": "0.006", "reasoning": "Trades are performing well."}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    with patch("src.ai_brain.ANTHROPIC_KEY", "test-key-123"), \
         patch("src.ai_brain.AsyncAnthropic", return_value=mock_client), \
         patch("src.ai_brain._load_recent_trades", return_value=[]), \
         patch("src.ai_brain.log_review", new_callable=AsyncMock):
        from src.ai_brain import review_trade
        trade_data = {
            "profit_pct": "0.8500",
            "profit_ratio": "0.0085",
            "input_xrp": "50.000000",
            "output_xrp": "50.425000",
            "dry_run": True,
        }
        await review_trade(trade_data)

    mock_client.messages.create.assert_called_once()
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"
    assert call_kwargs["max_tokens"] == 500


# ---------------------------------------------------------------------------
# Test 3: review_trade() parses valid JSON response into AIReview dataclass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_trade_parses_valid_json_response():
    """AI-03: Parses Claude response into AIReview with suggestion, new_threshold, reasoning."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text='{"suggestion": "increase threshold", "new_threshold": "0.008", "reasoning": "Win rate is consistently high."}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    with patch("src.ai_brain.ANTHROPIC_KEY", "test-key-123"), \
         patch("src.ai_brain.AsyncAnthropic", return_value=mock_client), \
         patch("src.ai_brain._load_recent_trades", return_value=[]), \
         patch("src.ai_brain.log_review", new_callable=AsyncMock):
        from src.ai_brain import review_trade
        trade_data = {
            "profit_pct": "0.8500",
            "profit_ratio": "0.0085",
            "input_xrp": "50.000000",
            "output_xrp": "50.425000",
            "dry_run": True,
        }
        result = await review_trade(trade_data)

    assert result is not None
    assert result.suggestion == "increase threshold"
    assert result.new_threshold == "0.008"
    assert result.reasoning == "Win rate is consistently high."
    assert result.model == "claude-haiku-4-5"
    assert result.trade_profit_pct == "0.8500"


# ---------------------------------------------------------------------------
# Test 4: review_trade() handles malformed Claude response gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_trade_handles_malformed_response():
    """Malformed/non-JSON response is handled gracefully — logs warning, returns None."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="Sorry, I cannot provide analysis right now.")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_message)

    with patch("src.ai_brain.ANTHROPIC_KEY", "test-key-123"), \
         patch("src.ai_brain.AsyncAnthropic", return_value=mock_client), \
         patch("src.ai_brain._load_recent_trades", return_value=[]):
        from src.ai_brain import review_trade
        trade_data = {
            "profit_pct": "0.8500",
            "profit_ratio": "0.0085",
            "input_xrp": "50.000000",
            "output_xrp": "50.425000",
            "dry_run": True,
        }
        result = await review_trade(trade_data)

    assert result is None


# ---------------------------------------------------------------------------
# Test 5: review_trade() retries on API error with exponential backoff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_trade_retries_on_api_error():
    """Retries 3 times with exponential backoff, then returns None without raising."""
    import anthropic as anthropic_module

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic_module.APIConnectionError(request=MagicMock())
    )

    with patch("src.ai_brain.ANTHROPIC_KEY", "test-key-123"), \
         patch("src.ai_brain.AsyncAnthropic", return_value=mock_client), \
         patch("src.ai_brain._load_recent_trades", return_value=[]), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        from src.ai_brain import review_trade
        trade_data = {
            "profit_pct": "0.8500",
            "profit_ratio": "0.0085",
            "input_xrp": "50.000000",
            "output_xrp": "50.425000",
            "dry_run": True,
        }
        result = await review_trade(trade_data)

    # Should return None after all retries exhausted, never raise
    assert result is None
    # Should have tried 3 times
    assert mock_client.messages.create.call_count == 3


# ---------------------------------------------------------------------------
# Test 6: review_trade() loads last 50 trades from LOG_FILE for context
# ---------------------------------------------------------------------------

def test_load_recent_trades_reads_last_n_lines(tmp_path):
    """AI-02: Loads last 50 trades from LOG_FILE for context."""
    log_file = tmp_path / "test_trades.jsonl"
    # Write 60 trade entries
    entries = []
    for i in range(60):
        entry = {
            "timestamp": f"2026-04-10T{i:02d}:00:00Z",
            "profit_pct": str(round(0.6 + i * 0.01, 4)),
            "profit_ratio": str(round(0.006 + i * 0.0001, 6)),
            "input_xrp": "50.0",
            "output_xrp": str(50.3 + i * 0.1),
            "dry_run": True,
        }
        entries.append(entry)
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    from src.ai_brain import _load_recent_trades
    trades = _load_recent_trades(str(log_file), count=50)

    # Should return last 50 (entries 10-59), not all 60
    assert len(trades) == 50
    # Last entry should be the most recent (index 59)
    assert trades[-1]["profit_pct"] == str(round(0.6 + 59 * 0.01, 4))


def test_load_recent_trades_handles_missing_file():
    """Returns empty list if LOG_FILE does not exist."""
    from src.ai_brain import _load_recent_trades
    result = _load_recent_trades("/nonexistent/path/trades.jsonl")
    assert result == []


def test_load_recent_trades_skips_malformed_lines(tmp_path):
    """Skips lines that are not valid JSON without crashing."""
    log_file = tmp_path / "trades.jsonl"
    log_file.write_text(
        '{"profit_pct": "0.8", "dry_run": true}\n'
        "NOT VALID JSON\n"
        '{"profit_pct": "0.9", "dry_run": true}\n'
    )
    from src.ai_brain import _load_recent_trades
    trades = _load_recent_trades(str(log_file))
    assert len(trades) == 2


# ---------------------------------------------------------------------------
# Test 7: log_review() appends AIReview to ai_reviews.jsonl with timestamp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_review_appends_to_file(tmp_path):
    """log_review() appends a review entry with timestamp to AI_REVIEWS_FILE."""
    reviews_file = tmp_path / "ai_reviews.jsonl"

    from src.ai_brain import AIReview, log_review
    review = AIReview(
        suggestion="hold steady",
        new_threshold="0.006",
        reasoning="Performance is acceptable.",
        model="claude-haiku-4-5",
        trade_profit_pct="0.8500",
    )
    trade_data = {"profit_pct": "0.8500", "dry_run": True}

    with patch("src.ai_brain.AI_REVIEWS_FILE", str(reviews_file)):
        await log_review(review, trade_data)

    assert reviews_file.exists()
    with open(reviews_file) as f:
        line = f.readline()
    entry = json.loads(line)
    assert "timestamp" in entry
    assert entry["suggestion"] == "hold steady"
    assert entry["new_threshold"] == "0.006"
    assert entry["reasoning"] == "Performance is acceptable."
    assert entry["model"] == "claude-haiku-4-5"
    assert entry["trade_profit_pct"] == "0.8500"


# ---------------------------------------------------------------------------
# Test 8: _build_prompt() includes current trade data and recent trade summaries
# ---------------------------------------------------------------------------

def test_build_prompt_includes_trade_data_and_context():
    """_build_prompt() includes current trade and summary of recent trades."""
    from src.ai_brain import _build_prompt
    trade_data = {
        "profit_pct": "0.8500",
        "profit_ratio": "0.0085",
        "input_xrp": "50.000000",
        "output_xrp": "50.425000",
        "dry_run": True,
    }
    recent_trades = [
        {"profit_pct": "0.7", "profit_ratio": "0.007", "dry_run": True},
        {"profit_pct": "0.8", "profit_ratio": "0.008", "dry_run": True},
        {"profit_pct": "-0.1", "profit_ratio": "-0.001", "dry_run": True},
    ]
    prompt = _build_prompt(trade_data, recent_trades)

    # Must include the current trade data
    assert "0.8500" in prompt
    # Must include count of recent trades
    assert "3" in prompt
    # Must include the observe-only safety statement
    assert "human review only" in prompt.lower() or "not be auto-applied" in prompt.lower() or "observe-only" in prompt.lower()
    # Must request JSON format
    assert "suggestion" in prompt
    assert "new_threshold" in prompt
    assert "reasoning" in prompt
