---
phase: 02-backtester-ai-brain
reviewed: 2026-04-10T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - main.py
  - src/ai_brain.py
  - src/config.py
  - tests/test_ai_brain.py
  - src/backtester.py
  - backtest.py
  - tests/test_backtester.py
findings:
  critical: 0
  warning: 3
  info: 4
  total: 7
status: issues_found
---

# Phase 02: Code Review Report

**Reviewed:** 2026-04-10T00:00:00Z
**Depth:** standard
**Files Reviewed:** 7
**Status:** issues_found

## Summary

Seven files reviewed covering the backtester engine, AI brain integration, CLI entry point, config module, and their tests. No critical issues (no security vulnerabilities, no data loss paths, no auth bypasses). Three warnings were found: a logic error in `last_n=0` handling in the backtester, an inconsistent missing TLS validation for the RPC URL in config, and a test mock that likely does not intercept the actual `asyncio.sleep` call causing slow test runs. Four informational items cover float usage in AI prompt math (against project Decimal policy), silent `asyncio.create_task` orphaning in main, missing exception tracebacks in the scan error handler, and a JSON fence-stripping gap in the AI response parser.

---

## Warnings

### WR-01: `last_n=0` Returns All Trades Instead of Zero Trades

**File:** `src/backtester.py:70`

**Issue:** The guard `if self.last_n is not None and self.last_n > 0` skips slicing when `last_n=0`. A caller passing `last_n=0` likely intends to get no trades (or is using 0 as a sentinel meaning "disabled"), but instead receives the full trade list. This is a silent logic inversion — no error is raised and the caller has no way to detect the miscommunication.

**Fix:**
```python
# Option A: treat last_n=0 as "no limit" (document it clearly)
if self.last_n is not None and self.last_n > 0:
    trades = trades[-self.last_n:]

# Option B: treat last_n=0 as "return nothing" (matches zero-means-empty semantics)
if self.last_n is not None:
    trades = trades[-self.last_n:] if self.last_n > 0 else []
```

Pick one and document it in the `__init__` docstring. The CLI in `backtest.py` only ever passes `None` or a positive int, but a future caller could easily pass 0.

---

### WR-02: `XRPL_RPC_URL` Has No TLS Validation

**File:** `src/config.py:20-21`

**Issue:** `XRPL_WS_URL` is validated to require `wss://` at startup (with a clear threat reference T-01-01), but `XRPL_RPC_URL` has no equivalent check. A misconfigured `http://` RPC URL would silently send account data and transaction details in plaintext. The asymmetry also makes the codebase feel inconsistent — anyone reading config.py will reasonably assume both URLs are validated.

**Fix:**
```python
_rpc_url = os.getenv("XRPL_RPC_URL", "https://s1.ripple.com")
if not _rpc_url.startswith("https://"):
    raise ValueError(
        f"XRPL_RPC_URL must use https:// (TLS) — got: {_rpc_url!r}. "
        "Plain http:// connections are not allowed."
    )
XRPL_RPC_URL: str = _rpc_url
```

---

### WR-03: `asyncio.sleep` Patch in Test Does Not Intercept Actual Calls

**File:** `tests/test_ai_brain.py:153`

**Issue:** The test patches `asyncio.sleep` at the global module level (`patch("asyncio.sleep", ...)`), but `ai_brain.py` imports asyncio as `import asyncio` and calls `await asyncio.sleep(delay)`. Python's `unittest.mock.patch` replaces the attribute on the named object, so the correct target is `src.ai_brain.asyncio.sleep`. As written, the patch replaces `asyncio.sleep` in the `asyncio` module itself, which may or may not intercept the call depending on Python version and import caching. In practice this often fails silently — the sleep actually executes and the test takes 1 + 2 = 3 seconds to run (delays for attempts 1 and 2 before the final exhaustion log).

**Fix:**
```python
with patch("src.ai_brain.asyncio.sleep", new_callable=AsyncMock):
    ...
```

---

## Info

### IN-01: Float Arithmetic Used for AI Prompt Stats (Violates Decimal Policy)

**File:** `src/ai_brain.py:113-123`

**Issue:** `_build_prompt` accumulates `profit_sum` as a Python `float` and computes `win_rate` and `avg_profit` as floats. The project's CLAUDE.md explicitly states "All monetary calculations use `decimal.Decimal` — no floating point." These values are only embedded in a display string sent to Claude for analysis (not used in trade decisions), so this does not affect trade correctness. However it violates the stated convention and could cause subtle display rounding for high-volume logs.

**Fix:**
```python
profit_sum = Decimal("0")
for t in recent_trades:
    try:
        ratio = Decimal(str(t.get("profit_ratio", "0")))
        if ratio > Decimal("0"):
            wins += 1
        profit_sum += Decimal(str(t.get("profit_pct", "0")))
    except (InvalidOperation, TypeError, ValueError):
        pass

total_d = Decimal(str(total)) if total > 0 else Decimal("1")
win_rate = (Decimal(str(wins)) / total_d * Decimal("100")) if total > 0 else Decimal("0")
avg_profit = profit_sum / total_d if total > 0 else Decimal("0")
```

Add `from decimal import Decimal, InvalidOperation` to the imports (it is already imported from `src.config` indirectly, but should be explicit in this module).

---

### IN-02: `asyncio.create_task` Result Not Retained — Orphaned Task Warning Risk

**File:** `main.py:115`

**Issue:** `asyncio.create_task(review_trade(trade_review_data))` discards the returned `Task` object. Python's asyncio logs a `Task was destroyed but it is pending!` warning if the event loop closes while tasks are outstanding. This is expected for fire-and-forget patterns, but the discarded reference also means the task can be garbage-collected immediately in some CPython GC scenarios, cancelling it silently before it runs.

**Fix:** Hold a reference to prevent premature GC, even if you never await it:
```python
# At module or function scope, maintain a set of background tasks
_background_tasks: set = set()

# In on_ledger_close:
task = asyncio.create_task(review_trade(trade_review_data))
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
```

This pattern is documented in the Python asyncio docs for fire-and-forget tasks.

---

### IN-03: Scan Error Handler Drops Exception Traceback

**File:** `main.py:126`

**Issue:** `logger.error(f"Scan error at ledger {ledger_index}: {e}")` only logs the exception message string. The traceback — which identifies the exact file, line, and call chain — is lost. When debugging production issues, the traceback is usually the most important piece of information.

**Fix:**
```python
logger.exception(f"Scan error at ledger {ledger_index}")
# logger.exception() automatically includes exc_info=True
```

Or equivalently: `logger.error(f"Scan error at ledger {ledger_index}: {e}", exc_info=True)`

---

### IN-04: AI Response Parser Does Not Strip Markdown Code Fences

**File:** `src/ai_brain.py:166`

**Issue:** `_parse_response` calls `json.loads(response_text.strip())` directly. Claude occasionally wraps JSON responses in markdown code fences (` ```json\n{...}\n``` `). When this happens, `json.loads` raises `JSONDecodeError`, the function returns `None`, and a warning is logged. The bot continues normally, but the AI review is silently dropped. This may be more common than expected since the prompt says "ONLY valid JSON" — Claude sometimes still adds fences.

**Fix:**
```python
def _parse_response(response_text: str) -> Optional[AIReview]:
    text = response_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first line (```json or ```) and last line (```)
        text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
        ...
```

---

_Reviewed: 2026-04-10T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
