# Requirements: XRPL 99%+ Arbitrage Bot

**Defined:** 2026-04-10
**Core Value:** Every executed trade must be mathematically near-certain profitable -- the bot never submits a transaction that hasn't passed live ledger simulation with profit above threshold.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Core Bot Engine

- [x] **BOT-01**: Bot connects to XRPL mainnet via WebSocket and maintains persistent connection with auto-reconnect
- [x] **BOT-02**: Bot scans for arbitrage opportunities using `ripple_path_find` (hybrid AMM + CLOB routing)
- [x] **BOT-03**: Profit calculation uses `decimal.Decimal` with formula: `((SimulatedOutput - Input) / Input) - NetworkFee - SlippageBuffer > 0.006`
- [x] **BOT-04**: SlippageBuffer base is 0.003 (0.3%), dynamically adjustable with 5-min volatility factor
- [x] **BOT-05**: Every candidate trade is validated via `simulate` RPC (live ledger dry-run) before submission
- [x] **BOT-06**: Only trades returning `tesSUCCESS` from simulation proceed to execution
- [x] **BOT-07**: Bot scans approximately once per ledger close (~3-5 seconds)

### Paper Trading (DRY_RUN)

- [x] **DRY-01**: `DRY_RUN=True` mode logs "would execute" trades without submitting transactions
- [x] **DRY-02**: Paper trading uses real mainnet ledger data and real `simulate` RPC results
- [x] **DRY-03**: All paper trades are logged identically to live trades (with `dry_run: true` flag)
- [x] **DRY-04**: DRY_RUN mode is the default -- requires explicit change to go live

### Circuit Breakers & Safety

- [x] **SAFE-01**: Max position size enforced at 5% of current account balance before simulation
- [x] **SAFE-02**: Daily loss circuit breaker at 2% of account -- bot pauses for 24 hours if hit
- [x] **SAFE-03**: Path/token blacklist prevents trading on known-bad or manipulated routes
- [x] **SAFE-04**: All financial math uses `decimal.Decimal` -- no floating point anywhere
- [x] **SAFE-05**: Wallet seed loaded from `.env` file only -- never hardcoded

### Live Execution

- [x] **LIVE-01**: Live trades use `autofill_and_sign` then `sign_and_submit` via xrpl-py
- [x] **LIVE-02**: Post-trade validation confirms on-ledger result matches simulation expectation
- [x] **LIVE-03**: Failed live submissions are logged with full error details

### Telegram Alerts

- [x] **TELE-01**: Telegram bot sends alert on every opportunity detected (paper or live)
- [x] **TELE-02**: Alerts include profit percentage, input/output amounts, and trade mode
- [x] **TELE-03**: Telegram credentials loaded from `.env` -- bot works without Telegram configured (graceful skip)

### Logging

- [x] **LOG-01**: All trades logged to `xrpl_arb_log.jsonl` in append-only JSON Lines format
- [x] **LOG-02**: Each log entry includes: timestamp, profit_pct, input_xrp, simulated_output, dry_run flag, and hash (if live)
- [x] **LOG-03**: Console logging uses Python standard logging with timestamps and levels
- [x] **LOG-04**: Log file is shared between bot and Streamlit dashboard

### Backtesting

- [x] **BACK-01**: Backtesting module replays pathfinding logic against historical ledger data
- [x] **BACK-02**: Backtest reports win rate, total opportunities, and average profit per opportunity
- [x] **BACK-03**: Backtest is runnable standalone via `python backtest.py`

### AI Brain

- [x] **AI-01**: Async Claude API review runs after every trade (paper or live) -- never blocks main loop
- [x] **AI-02**: AI receives current trade data plus last 50 trades for pattern analysis
- [x] **AI-03**: AI returns structured JSON: suggestion, new_threshold recommendation, and reasoning
- [x] **AI-04**: AI brain is optional -- bot works fully without ANTHROPIC_KEY configured

### Streamlit Dashboard

- [x] **UI-01**: Real-time dashboard shows win rate, total opportunities, and average profit
- [x] **UI-02**: Dashboard displays recent 20 opportunities in a data table
- [x] **UI-03**: Profit distribution histogram using Plotly
- [x] **UI-04**: Auto-refreshes every 5 seconds from `xrpl_arb_log.jsonl`
- [x] **UI-05**: Graceful empty state when no logs exist yet

### Deployment & VPS

- [x] **DEP-01**: systemd service file runs bot as `xrplbot` non-root user with `Restart=always`
- [x] **DEP-02**: Service file includes resource limits appropriate for 1-core / 4GB VPS
- [x] **DEP-03**: `.env.example` documents all required and optional environment variables
- [x] **DEP-04**: README includes complete Hostinger-specific deployment guide (SSH, user creation, package install, service setup)
- [x] **DEP-05**: Deployment instructions ensure coexistence with OpenClaw Docker project
- [x] **DEP-06**: README includes 7-day paper-trading review criteria checklist
- [x] **DEP-07**: README includes instructions to switch from paper to live with minimal capital (10-20 XRP)

### Atomic Two-Leg Submit (Phase 5)

- [ ] **ATOM-01**: Both legs of an arbitrage trade are fully built and signed BEFORE leg 1 is submitted to the network
- [ ] **ATOM-02**: Legs use sequential `Sequence` numbers (N, N+1) so they can apply in the same or adjacent ledger
- [ ] **ATOM-03**: Leg 2 is submitted immediately after leg 1 submission returns (no wait for leg 1 ledger validation)
- [ ] **ATOM-04**: If leg 1 fails with a terminal code (tec*/tef*/tem*), leg 2 is cancelled (or its Sequence deliberately burned via no-op) so no orphaned signed tx can be replayed later
- [ ] **ATOM-05**: If leg 2 fails AFTER leg 1 commits on-ledger, existing 2% market-dump recovery flow activates (current behavior preserved)
- [ ] **ATOM-06**: Pre-submission check ensures the wallet account is the single writer during the arb window (no concurrent tx consuming the pre-signed Sequence numbers)
- [ ] **ATOM-07**: `simulate` RPC runs for BOTH legs before leg 1 submission; leg 2 returning `terPRE_SEQ` is treated as pass (state-dependent, expected before leg 1 commits)
- [ ] **ATOM-08**: All financial math in the two-leg flow continues to use `decimal.Decimal` (SAFE-04 preserved)
- [ ] **ATOM-09**: Bot logs each leg's submit result, Sequence number, and ledger index to `xrpl_arb_log.jsonl` for post-trade audit
- [ ] **ATOM-10**: Atomic submit is the default execution path when `DRY_RUN=False`; the previous sequential-submit path is removed (no dead code)

### Currency Universe Expansion (Phase 5)

- [ ] **CURR-01**: `HIGH_LIQ_CURRENCIES` is expanded beyond `USD,USDC,RLUSD,EUR` to include additional liquid XRPL currencies (at minimum: SOLO plus one more, chosen during research)
- [ ] **CURR-02**: Adding or removing a currency requires only an `.env` config change — no code edits
- [ ] **CURR-03**: Every currency in `HIGH_LIQ_CURRENCIES` has at least one trusted issuer address configured and documented in `.env.example`

### Dead-Knob Cleanup (Phase 5)

- [ ] **CLEAN-01**: `LEG2_TIMEOUT_LEDGERS` is either wired into `src/executor.py` (replacing the hardcoded `_LEDGER_WINDOW = 4`) OR removed from `src/config.py` and `.env.example`
- [ ] **CLEAN-02**: `PROFIT_THRESHOLD_LOW_LIQ` is either returned by `get_profit_threshold()` for non-HIGH_LIQ currencies OR removed from `src/config.py` and `src/profit_math.py`

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Advanced Features

- **ADV-01**: Dynamic slippage buffer using real-time 5-minute volatility calculation
- **ADV-02**: Multi-currency arbitrage paths (not just XRP-to-XRP loops)
- **ADV-03**: Automatic AI-driven threshold adjustment based on brain suggestions
- **ADV-04**: Nginx reverse proxy for Streamlit with basic auth
- **ADV-05**: Prometheus/Grafana monitoring integration
- **ADV-06**: Webhook-based alerting (Discord, Slack) in addition to Telegram

## Out of Scope

| Feature | Reason |
|---------|--------|
| Testnet mode | Paper trading uses real mainnet + simulate RPC -- testnet unnecessary |
| High-frequency / sub-second trading | Not needed for 99% win rate strategy; would stress 1-core VPS |
| Multi-wallet support | Single wallet is simpler and safer for v1 |
| Trade execution via Streamlit UI | Dashboard is read-only; execution is bot-only |
| Docker deployment for bot | systemd is simpler; Docker reserved for OpenClaw |
| Automated capital scaling | Manual scaling after paper-trading validation is safer |
| OAuth/authentication on dashboard | Local/VPS-only access; add auth in v2 if needed |
| Windows deployment | Target is Linux VPS only |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| BOT-01 | Phase 1 | Complete |
| BOT-02 | Phase 1 | Complete |
| BOT-03 | Phase 1 | Complete |
| BOT-04 | Phase 1 | Complete |
| BOT-05 | Phase 1 | Complete |
| BOT-06 | Phase 1 | Complete |
| BOT-07 | Phase 1 | Complete |
| DRY-01 | Phase 1 | Complete |
| DRY-02 | Phase 1 | Complete |
| DRY-03 | Phase 1 | Complete |
| DRY-04 | Phase 1 | Complete |
| SAFE-01 | Phase 1 | Complete |
| SAFE-02 | Phase 1 | Complete |
| SAFE-03 | Phase 1 | Complete |
| SAFE-04 | Phase 1 | Complete |
| SAFE-05 | Phase 1 | Complete |
| LIVE-01 | Phase 1 | Complete |
| LIVE-02 | Phase 1 | Complete |
| LIVE-03 | Phase 1 | Complete |
| TELE-01 | Phase 1 | Complete |
| TELE-02 | Phase 1 | Complete |
| TELE-03 | Phase 1 | Complete |
| LOG-01 | Phase 1 | Complete |
| LOG-02 | Phase 1 | Complete |
| LOG-03 | Phase 1 | Complete |
| LOG-04 | Phase 1 | Complete |
| BACK-01 | Phase 2 | Complete |
| BACK-02 | Phase 2 | Complete |
| BACK-03 | Phase 2 | Complete |
| AI-01 | Phase 2 | Complete |
| AI-02 | Phase 2 | Complete |
| AI-03 | Phase 2 | Complete |
| AI-04 | Phase 2 | Complete |
| UI-01 | Phase 3 | Complete |
| UI-02 | Phase 3 | Complete |
| UI-03 | Phase 3 | Complete |
| UI-04 | Phase 3 | Complete |
| UI-05 | Phase 3 | Complete |
| DEP-01 | Phase 4 | Complete |
| DEP-02 | Phase 4 | Complete |
| DEP-03 | Phase 4 | Complete |
| DEP-04 | Phase 4 | Complete |
| DEP-05 | Phase 4 | Complete |
| DEP-06 | Phase 4 | Complete |
| DEP-07 | Phase 4 | Complete |
| ATOM-01 | Phase 5 | Planned |
| ATOM-02 | Phase 5 | Planned |
| ATOM-03 | Phase 5 | Planned |
| ATOM-04 | Phase 5 | Planned |
| ATOM-05 | Phase 5 | Planned |
| ATOM-06 | Phase 5 | Planned |
| ATOM-07 | Phase 5 | Planned |
| ATOM-08 | Phase 5 | Planned |
| ATOM-09 | Phase 5 | Planned |
| ATOM-10 | Phase 5 | Planned |
| CURR-01 | Phase 5 | Planned |
| CURR-02 | Phase 5 | Planned |
| CURR-03 | Phase 5 | Planned |
| CLEAN-01 | Phase 5 | Planned |
| CLEAN-02 | Phase 5 | Planned |

**Coverage:**
- v1 requirements: 42 total (all complete)
- Phase 5 requirements: 15 total (all planned)
- Unmapped: 0

---
*Requirements defined: 2026-04-10*
*Last updated: 2026-04-10 after initial definition*
