# Roadmap: XRPL 99%+ Arbitrage Bot

## Overview

Four phases take this project from zero to a live VPS-deployed arbitrage bot. Phase 1 builds the complete core engine — connection, pathfinding, profit math, simulation gating, DRY_RUN mode, circuit breakers, Telegram, and logging. Phase 2 adds intelligence — backtesting against historical data and an async Claude AI brain for post-trade pattern review. Phase 3 wraps it in a Streamlit read-only dashboard so you can watch the bot work in real time. Phase 4 ships it to Hostinger — systemd service, non-root user, coexistence with OpenClaw, and a complete deployment guide.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Core Bot Engine** - Working bot that scans, simulates, and executes (or paper-trades) with full safety controls (completed 2026-04-10)
- [x] **Phase 2: Backtester + AI Brain** - Historical replay backtesting and async Claude post-trade pattern analysis (completed 2026-04-10)
- [x] **Phase 3: Streamlit Dashboard** - Real-time read-only web dashboard fed from the shared JSONL log (completed 2026-04-10)
- [x] **Phase 4: Deployment** - systemd service, non-root user, .env.example, README, and Hostinger coexistence guide (completed 2026-04-10)
- [ ] **Phase 5: Atomic Two-Leg Submit + Currency Expansion** - Pre-sign both legs and submit back-to-back with sequential Sequence numbers to eliminate the 5-7s inter-leg drift that caused the 2026-04-19 live-trade losses; expand HIGH_LIQ_CURRENCIES beyond USD/USDC/RLUSD/EUR

## Phase Details

### Phase 1: Core Bot Engine
**Goal**: The bot runs on mainnet, scans for arbitrage opportunities, validates every candidate through live ledger simulation, and either logs a paper trade or executes a live one — with circuit breakers and Telegram alerts throughout
**Depends on**: Nothing (first phase)
**Requirements**: BOT-01, BOT-02, BOT-03, BOT-04, BOT-05, BOT-06, BOT-07, DRY-01, DRY-02, DRY-03, DRY-04, SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05, LIVE-01, LIVE-02, LIVE-03, TELE-01, TELE-02, TELE-03, LOG-01, LOG-02, LOG-03, LOG-04
**Success Criteria** (what must be TRUE):
  1. Bot connects to XRPL mainnet and logs a heartbeat every ledger close (~3-5 seconds) without dropping
  2. Running with DRY_RUN=True, the bot finds an opportunity and logs a "would execute" entry to xrpl_arb_log.jsonl with correct profit math
  3. No trade proceeds to execution (paper or live) unless simulate RPC returns tesSUCCESS
  4. Bot sends a Telegram alert when an opportunity is detected, including profit percentage and amounts
  5. Bot halts scanning for 24 hours if cumulative daily loss reaches 2% of account balance
**Plans**: 5 plans

Plans:
- [x] 01-01: XRPL connection layer — WebSocket client with auto-reconnect and ledger-close stream
- [x] 01-02: Pathfinder + profit math — ripple_path_find integration, Decimal profit formula, slippage buffer, position sizing
- [x] 01-03: Simulation gate + execution — simulate RPC validation, DRY_RUN mode, autofill_and_sign live path, post-trade validation
- [x] 01-04: Safety systems — circuit breakers, blacklist, SAFE-04 Decimal enforcement, SAFE-05 .env seed loading
- [x] 01-05: Telegram + logging — alert formatting, JSONL log writer, console logging

### Phase 2: Backtester + AI Brain
**Goal**: Historical data can be replayed to measure strategy win rate, and every executed trade (paper or live) gets an async Claude review that suggests threshold adjustments without blocking the main loop
**Depends on**: Phase 1
**Requirements**: BACK-01, BACK-02, BACK-03, AI-01, AI-02, AI-03, AI-04
**Success Criteria** (what must be TRUE):
  1. Running `python backtest.py` produces a report showing win rate, total opportunities found, and average profit per opportunity
  2. After every paper trade, the bot fires an async Claude call and logs the AI response (suggestion, new_threshold recommendation, reasoning) without slowing ledger scanning
  3. Bot continues operating normally when ANTHROPIC_KEY is absent from .env
**Plans**: 2 plans

Plans:
- [x] 02-01: Backtesting module — historical ledger data replay, pathfinding replay, win rate and profit report
- [x] 02-02: AI brain integration — async Claude API call after each trade, structured JSON response parsing, optional/graceful-skip behavior

### Phase 3: Streamlit Dashboard
**Goal**: A browser-based read-only dashboard auto-refreshes from the shared JSONL log and shows the bot's live win rate, recent opportunities, and profit distribution
**Depends on**: Phase 1
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05
**Success Criteria** (what must be TRUE):
  1. Opening the dashboard in a browser shows current win rate, total opportunities, and average profit — updated automatically every 5 seconds
  2. A table of the 20 most recent opportunities is visible with all relevant fields
  3. A Plotly profit distribution histogram renders correctly from real log data
  4. Dashboard shows a clean empty state message when xrpl_arb_log.jsonl does not exist yet
**Plans**: 2 plans

Plans:
- [x] 03-01: Streamlit app — log reader, metrics calculations, auto-refresh, empty state handling
- [x] 03-02: Charts and table — Plotly histogram, recent opportunities data table, layout polish
**UI hint**: yes

### Phase 4: Deployment
**Goal**: The bot runs as a hardened systemd service under a non-root user on the Hostinger VPS, coexists with OpenClaw Docker, and a complete README guides anyone through the full setup from SSH to live trading
**Depends on**: Phase 3
**Requirements**: DEP-01, DEP-02, DEP-03, DEP-04, DEP-05, DEP-06, DEP-07
**Success Criteria** (what must be TRUE):
  1. `sudo systemctl start xrplbot` starts the bot as the xrplbot non-root user with Restart=always and appropriate VPS resource limits
  2. The bot and OpenClaw Docker project run simultaneously on Hostinger KVM 1 without port conflicts or resource contention
  3. A new user can follow README alone — from SSH login through 7-day paper trading review to switching to live with 10-20 XRP
  4. .env.example documents every environment variable the bot reads, with inline explanations
**Plans**: 2 plans

Plans:
- [x] 04-01: systemd service + user setup — xrplbot user creation, service file with resource limits, enable/start workflow
- [x] 04-02: .env.example + README — all env vars documented, Hostinger deployment guide, OpenClaw coexistence notes, 7-day paper review checklist, live-trading switchover instructions

### Phase 5: Atomic Two-Leg Submit + Currency Expansion
**Goal**: The bot pre-signs BOTH legs of an arbitrage trade before submitting leg 1, uses sequential Sequence numbers (N, N+1) so both legs apply in the same or adjacent ledger, eliminating the 5-7s inter-leg drift window that caused 4 consecutive tecPATH_PARTIAL live-trade losses on 2026-04-19. Also expands HIGH_LIQ_CURRENCIES beyond the current USD/USDC/RLUSD/EUR to widen the opportunity net during calm market regimes. Dead config knobs (LEG2_TIMEOUT_LEDGERS, PROFIT_THRESHOLD_LOW_LIQ) are either wired in or removed.
**Depends on**: Phase 4
**Requirements**: ATOM-01, ATOM-02, ATOM-03, ATOM-04, ATOM-05, ATOM-06, ATOM-07, ATOM-08, ATOM-09, ATOM-10, CURR-01, CURR-02, CURR-03, CLEAN-01, CLEAN-02
**Success Criteria** (what must be TRUE):
  1. Both legs of an arbitrage trade are fully signed with sequential Sequence numbers BEFORE leg 1 is submitted to the network
  2. Leg 2 is submitted immediately after leg 1's submit call returns — no `ripple_path_find` re-run, no wait for leg 1 ledger validation
  3. If leg 1 fails terminally (tec*/tef*/tem*), leg 2 is cancelled or its Sequence deliberately burned so no orphaned signed transaction remains replayable
  4. Paper-trading replay against the 2026-04-19 incident data shows atomic submit would have succeeded on all 4 trades that failed under sequential submit
  5. `HIGH_LIQ_CURRENCIES` can be extended via `.env` alone (no code changes) and the new list is picked up on bot restart
  6. Both dead knobs (`LEG2_TIMEOUT_LEDGERS`, `PROFIT_THRESHOLD_LOW_LIQ`) are resolved — each either wired into live code paths or removed from config
  7. All existing 194 tests continue to pass, plus new tests cover both-succeed, leg-1-fail, leg-2-fail-after-leg-1-commit, and Sequence-burn scenarios
**Plans**: 5 plans

Plans:
- [ ] 05-01-PLAN.md — Config additions (LEG2_TIMEOUT_LEDGERS, PROFIT_THRESHOLD_LOW_LIQ 3-tier) + HIGH_LIQ currency expansion (SOLO, USDT) + issuer docs
- [ ] 05-02-PLAN.md — Simulator terPRE_SEQ acceptance helper (is_acceptable_sim_result + LEG2_ACCEPTABLE_CODES)
- [ ] 05-03-PLAN.md — Atomic two-leg executor rewrite (pre-sign + pre-sim + sequential submit + Sequence-burn orphan handling)
- [ ] 05-04-PLAN.md — Atomic executor test suite (happy path, failure paths, single-writer, Decimal, per-leg logs)
- [ ] 05-05-PLAN.md — Replay harness for 2026-04-19 incident (4 hashes via @pytest.mark.replay parametrization)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Bot Engine | 5/5 | Complete   | 2026-04-10 |
| 2. Backtester + AI Brain | 2/2 | Complete   | 2026-04-10 |
| 3. Streamlit Dashboard | 2/2 | Complete   | 2026-04-10 |
| 4. Deployment | 2/2 | Complete   | 2026-04-10 |
| 5. Atomic Two-Leg Submit + Currency Expansion | 0/5 | Planning   | -          |
