---
phase: 02-backtester-ai-brain
plan: 02
subsystem: ai-brain
tags: [ai, claude, async, fire-and-forget, jsonl, tdd]
dependency_graph:
  requires: [src/config.py, src/trade_logger.py, src/executor.py]
  provides: [src/ai_brain.py, ai_reviews.jsonl]
  affects: [main.py]
tech_stack:
  added: [anthropic>=0.40.0 (AsyncAnthropic client)]
  patterns: [asyncio.create_task fire-and-forget, exponential backoff retry, graceful skip pattern, append-only JSONL logging]
key_files:
  created: [src/ai_brain.py, tests/test_ai_brain.py]
  modified: [src/config.py, main.py]
decisions:
  - "AsyncAnthropic client used for non-blocking HTTP — avoids event loop blocking on API calls"
  - "Exponential backoff delays [1, 2, 4]s with 3 max retries — prevents retry storms (T-02-06)"
  - "AI suggestions observe-only — no code path modifies PROFIT_THRESHOLD from AI output (T-02-07)"
  - "ANTHROPIC_KEY never logged — only referenced as 'AI brain' in debug messages (T-02-04)"
  - "asyncio.create_task pattern (not await) — true fire-and-forget so scanner loop is never blocked (AI-01)"
metrics:
  duration: 3m
  completed: "2026-04-10"
  tasks_completed: 2
  files_modified: 4
requirements_satisfied: [AI-01, AI-02, AI-03, AI-04]
---

# Phase 02 Plan 02: AI Brain Module Summary

**One-liner:** Async Claude API integration with graceful skip, exponential backoff, JSONL logging, and fire-and-forget wiring via asyncio.create_task.

## What Was Built

An async AI brain (`src/ai_brain.py`) that reviews every executed trade by calling the Claude claude-haiku-4-5 model via the Anthropic SDK, without ever blocking the main XRPL scanning loop. The module is wired into `main.py` using `asyncio.create_task` — true fire-and-forget so the scanner loop continues immediately while the AI review runs in the background.

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | Create AI brain module with TDD | 1119b0b | src/ai_brain.py, tests/test_ai_brain.py, src/config.py |
| 2 | Wire AI brain into main loop | 6cb25b7 | main.py |

## Implementation Details

### src/ai_brain.py

- `AIReview` dataclass: suggestion, new_threshold, reasoning, model, trade_profit_pct
- `_load_recent_trades(log_file, count=50)`: reads last N lines from LOG_FILE, skips malformed JSON per-line
- `_build_prompt(trade_data, recent_trades)`: builds structured prompt with current trade + stats summary (win rate, avg profit, trade count)
- `_parse_response(response_text)`: json.loads in try/except — malformed response returns None, never crashes (T-02-05)
- `log_review(review, trade_data)`: append-only JSONL to AI_REVIEWS_FILE with timestamp
- `review_trade(trade_data)`: full async flow — graceful skip, load context, build prompt, call API, retry with backoff, parse, log, return

### src/config.py additions

```python
ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_KEY", "")
AI_REVIEWS_FILE: str = os.getenv("AI_REVIEWS_FILE", "ai_reviews.jsonl")
```

### main.py wiring

```python
result = await executor.execute(opp)
if result:
    # Fire-and-forget AI review — never blocks scanning (AI-01)
    trade_review_data = { ... }
    asyncio.create_task(review_trade(trade_review_data))
```

## Test Coverage

10 tests in `tests/test_ai_brain.py`, all passing:

1. `test_review_trade_skips_when_no_api_key` — AI-04 graceful skip
2. `test_review_trade_calls_correct_model` — model=claude-haiku-4-5, max_tokens=500
3. `test_review_trade_parses_valid_json_response` — AIReview fields populated correctly
4. `test_review_trade_handles_malformed_response` — non-JSON returns None
5. `test_review_trade_retries_on_api_error` — 3 retries, then None
6. `test_load_recent_trades_reads_last_n_lines` — returns last 50 of 60 entries
7. `test_load_recent_trades_handles_missing_file` — returns [] gracefully
8. `test_load_recent_trades_skips_malformed_lines` — skips bad JSON lines
9. `test_log_review_appends_to_file` — JSONL append with all fields + timestamp
10. `test_build_prompt_includes_trade_data_and_context` — prompt includes trade data, count, observe-only statement

## Threat Mitigations Applied

| Threat ID | Mitigation |
|-----------|------------|
| T-02-04 | ANTHROPIC_KEY loaded from .env only, never logged — debug messages say "AI brain" not the key |
| T-02-05 | _parse_response uses json.loads in try/except — malicious JSON returns None |
| T-02-06 | Exponential backoff [1,2,4]s + 3 retries + asyncio.create_task prevents DoS |
| T-02-07 | No code path from AI output to PROFIT_THRESHOLD — observe-only invariant |

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — AI brain is fully wired. (Will produce no output until ANTHROPIC_KEY is set in .env, which is expected and intentional per AI-04.)

## Threat Flags

None — no new network endpoints, auth paths, or schema changes beyond what the plan's threat model already covers.

## Self-Check: PASSED

All created files verified present. All task commits verified in git log.
