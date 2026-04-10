# Phase 2: Backtester + AI Brain - Context

**Gathered:** 2026-04-10
**Status:** Ready for planning

<domain>
## Phase Boundary

Build a backtesting module that replays paper trading logs to measure strategy performance, and an async AI brain that reviews every trade via Claude API without blocking the main scanning loop.

</domain>

<decisions>
## Implementation Decisions

### Backtesting Data Strategy
- Historical data source: Replay from `xrpl_arb_log.jsonl` (paper trading logs). No external API calls or re-simulation.
- Report output: Stdout human-readable summary + optional `backtest_report.json` machine-readable file
- No re-simulation of historical trades — replay logged results only (old ledger state is gone)

### AI Brain Behavior
- Rate limit/error handling: Exponential backoff with 3 retries, then skip. Never block the main loop.
- AI responses logged to separate `ai_reviews.jsonl` file (not mixed with trade logs)
- Model: claude-haiku-4-5 (cheapest, fastest, sufficient for pattern analysis on 1-core VPS)
- AI suggestions are observe-only in v1 — log recommendations but never auto-adjust thresholds

### Backtest CLI Interface
- CLI arguments: `--log-file` (path to JSONL, default `xrpl_arb_log.jsonl`) and `--last-n` (count, default all entries)
- Metrics in report: win rate, total opportunities, average profit, max profit, max loss, profit distribution

### Claude's Discretion
- Internal implementation details, module structure, and test approach are at Claude's discretion
- Follow patterns established in Phase 1 (TDD, Decimal math, async patterns)

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/trade_logger.py` — JSONL format definition, `log_trade()` function (append-only)
- `src/profit_math.py` — Decimal profit calculations, `is_profitable()`, `calculate_profit()`
- `src/config.py` — Environment variable loading pattern, Decimal conversion
- `src/pathfinder.py` — `Opportunity` dataclass with all trade fields

### Established Patterns
- All financial math uses `decimal.Decimal` — zero float
- Async throughout (asyncio)
- TDD: tests first (RED), then implementation (GREEN)
- Graceful skip when optional services unavailable (see `src/telegram_alerts.py`)

### Integration Points
- `xrpl_arb_log.jsonl` — shared log file between bot, backtester, and future dashboard
- `main.py` — AI brain hooks into the post-trade callback in the main loop
- `.env` — `ANTHROPIC_KEY` added as optional variable

</code_context>

<specifics>
## Specific Ideas

- Include a future-enhancement comment in `backtest.py`: "Future enhancement opportunity: Add optional historical ledger replay mode using XRPL API for broader testing beyond just paper trading logs."
- Keep everything lightweight for 1-core / 4GB RAM VPS

</specifics>

<deferred>
## Deferred Ideas

- Auto-adjustment of thresholds based on AI suggestions (ADV-03 in v2 requirements)
- Historical ledger API replay mode for backtesting
- Multi-currency arbitrage path analysis

</deferred>

---

*Phase: 02-backtester-ai-brain*
*Context gathered: 2026-04-10 via smart discuss (autonomous mode)*
