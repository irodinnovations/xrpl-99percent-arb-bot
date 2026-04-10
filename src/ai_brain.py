"""Async AI brain — reviews every trade via Claude API without blocking main loop.

AI-01: Fires after every trade (paper or live) as asyncio.create_task — never blocks scanning.
AI-02: Sends current trade data plus last 50 trades as context to Claude.
AI-03: Parses Claude response as structured JSON: suggestion, new_threshold, reasoning.
AI-04: Gracefully skips when ANTHROPIC_KEY is absent — bot operates fully without it.

Safety invariant: AI suggestions are observe-only — logged to ai_reviews.jsonl for
human review only. They will NOT be auto-applied. No code path modifies PROFIT_THRESHOLD
from AI output (T-02-07).

Key design decisions:
- AsyncAnthropic client: avoids blocking the event loop on HTTP calls
- Exponential backoff (1s, 2s, 4s, 3 attempts): prevents retry storms (T-02-06)
- json.loads in try/except: malformed/malicious JSON returns None, never crashes (T-02-05)
- ANTHROPIC_KEY never logged: only referenced as "AI brain" in debug messages (T-02-04)
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import anthropic
from anthropic import AsyncAnthropic

from src.config import ANTHROPIC_KEY, AI_REVIEWS_FILE, LOG_FILE

logger = logging.getLogger(__name__)

# Retry configuration (T-02-06: prevents retry storms)
_RETRY_DELAYS = [1, 2, 4]  # seconds between attempts
_MAX_RETRIES = 3
_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 500


@dataclass
class AIReview:
    """Structured AI review of a trade. Observe-only — never auto-applied (T-02-07, AI-03).

    Fields:
        suggestion: Human-readable suggestion (e.g., "increase threshold", "hold steady")
        new_threshold: Suggested threshold as Decimal string (e.g., "0.008")
        reasoning: Claude's explanation of the suggestion
        model: Model used for the review (always "claude-haiku-4-5")
        trade_profit_pct: Profit percentage of the trade that triggered this review
    """

    suggestion: str
    new_threshold: str
    reasoning: str
    model: str
    trade_profit_pct: str


def _load_recent_trades(log_file: str, count: int = 50) -> list[dict]:
    """Load the last `count` trades from LOG_FILE for AI context (AI-02).

    Reads from the end of the file so we get the most recent trades efficiently.
    Skips malformed lines without crashing — per-line try/except (mitigates T-02-01).

    Args:
        log_file: Path to the JSONL trade log file.
        count: Maximum number of recent trades to return.

    Returns:
        List of trade dicts, oldest first, up to `count` entries.
        Returns empty list if file does not exist or is unreadable.
    """
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, FileNotFoundError):
        logger.debug(f"Trade log not found or unreadable: {log_file}")
        return []

    trades = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping malformed trade log line")
            continue

    # Return only the last `count` entries
    return trades[-count:] if len(trades) > count else trades


def _build_prompt(trade_data: dict, recent_trades: list[dict]) -> str:
    """Build the Claude prompt with current trade and recent history summary (AI-02, AI-03).

    Computes win rate and average profit inline from recent_trades.
    Requests structured JSON response: suggestion, new_threshold, reasoning.
    Explicitly states suggestions are observe-only — NOT auto-applied (T-02-07).

    Args:
        trade_data: Current trade dict (profit_pct, input_xrp, output_xrp, etc.)
        recent_trades: List of recent trade dicts for pattern context.

    Returns:
        Full prompt string to send to Claude.
    """
    # Compute summary stats from recent trades inline
    total = len(recent_trades)
    wins = 0
    profit_sum = 0.0
    for t in recent_trades:
        try:
            ratio = float(t.get("profit_ratio", "0"))
            if ratio > 0:
                wins += 1
            profit_sum += float(t.get("profit_pct", "0"))
        except (ValueError, TypeError):
            pass

    win_rate = (wins / total * 100) if total > 0 else 0.0
    avg_profit = (profit_sum / total) if total > 0 else 0.0

    current_trade_json = json.dumps(trade_data, default=str, indent=2)

    prompt = f"""You are a trading strategy analyst reviewing XRPL arbitrage trades.
Analyze the current trade in context of recent trading history.
Respond with ONLY valid JSON.

IMPORTANT: Your suggestions are logged for human review only — they will NOT be auto-applied.
This is an observe-only advisory system. (T-02-07)

Current trade:
{current_trade_json}

Recent trading history summary ({total} trades):
- Win rate: {win_rate:.1f}%
- Average profit: {avg_profit:.4f}%
- Winning trades: {wins} of {total}

Please analyze this trade and respond with ONLY this JSON format:
{{
  "suggestion": "hold steady | increase threshold | decrease threshold | pause trading",
  "new_threshold": "0.XXX",
  "reasoning": "Your explanation here"
}}"""

    return prompt


def _parse_response(response_text: str) -> Optional[AIReview]:
    """Parse Claude's response text into an AIReview (AI-03, T-02-05).

    Uses json.loads in try/except — malformed or malicious JSON returns None,
    never crashes the bot. Only extracts expected string fields — ignores
    unexpected fields to prevent injection via response content.

    Args:
        response_text: Raw text from Claude API response.

    Returns:
        AIReview dataclass if parsing succeeds, None otherwise.
    """
    try:
        data = json.loads(response_text.strip())
        suggestion = str(data.get("suggestion", ""))
        new_threshold = str(data.get("new_threshold", ""))
        reasoning = str(data.get("reasoning", ""))

        if not suggestion or not new_threshold or not reasoning:
            logger.warning("AI response missing required fields — skipping")
            return None

        return AIReview(
            suggestion=suggestion,
            new_threshold=new_threshold,
            reasoning=reasoning,
            model=_MODEL,
            trade_profit_pct="",  # filled by review_trade caller
        )
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning(f"AI response parsing failed: {e} — skipping")
        return None


async def log_review(review: AIReview, trade_data: dict) -> None:
    """Append an AIReview to the AI reviews JSONL file (AI-03).

    Same append-only pattern as trade_logger.log_trade. Includes timestamp,
    all review fields, and the triggering trade's profit_pct for correlation.

    Args:
        review: The AIReview dataclass to log.
        trade_data: The trade dict that triggered this review (for context).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suggestion": review.suggestion,
        "new_threshold": review.new_threshold,
        "reasoning": review.reasoning,
        "model": review.model,
        "trade_profit_pct": review.trade_profit_pct,
        "trade_data": trade_data,
    }
    try:
        with open(AI_REVIEWS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        logger.debug(f"AI review logged to {AI_REVIEWS_FILE}")
    except OSError as e:
        logger.error(f"Failed to write AI review log: {e}")


async def review_trade(trade_data: dict) -> Optional[AIReview]:
    """Review a trade via Claude API — async, fire-and-forget safe (AI-01).

    Gracefully skips if ANTHROPIC_KEY is not configured (AI-04).
    Loads last 50 trades for context (AI-02).
    Parses structured JSON response (AI-03).
    Retries on API errors with exponential backoff: delays [1, 2, 4]s, 3 attempts (T-02-06).
    Entire function is wrapped in try/except — never raises, never blocks caller.

    ANTHROPIC_KEY is sent over HTTPS via the anthropic SDK only — never logged (T-02-04).
    AI suggestions are observe-only — this function never modifies thresholds (T-02-07).

    Args:
        trade_data: Dict with profit_pct, profit_ratio, input_xrp, output_xrp, dry_run.

    Returns:
        AIReview if successful, None if skipped or all retries exhausted.
    """
    # AI-04: Graceful skip if not configured (follows telegram_alerts.py pattern exactly)
    if not ANTHROPIC_KEY:
        logger.debug("AI brain not configured — skipping review (AI-04)")
        return None

    try:
        recent_trades = _load_recent_trades(LOG_FILE)
        prompt = _build_prompt(trade_data, recent_trades)

        client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        last_error: Optional[Exception] = None

        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                message = await client.messages.create(
                    model=_MODEL,
                    max_tokens=_MAX_TOKENS,
                    messages=[
                        {"role": "user", "content": prompt},
                    ],
                )
                response_text = message.content[0].text
                review = _parse_response(response_text)

                if review is not None:
                    # Attach the triggering trade's profit_pct for correlation
                    review.trade_profit_pct = str(trade_data.get("profit_pct", ""))
                    await log_review(review, trade_data)

                return review

            except (
                anthropic.APIConnectionError,
                anthropic.RateLimitError,
                anthropic.APIStatusError,
            ) as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        f"AI brain API error (attempt {attempt}/{_MAX_RETRIES}): {e} "
                        f"— retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        f"AI brain API error after {_MAX_RETRIES} attempts: {e} "
                        f"— skipping review"
                    )

        return None

    except Exception as e:
        # Catch-all: AI brain must never crash the main loop (AI-01)
        logger.error(f"AI brain unexpected error: {e}")
        return None
