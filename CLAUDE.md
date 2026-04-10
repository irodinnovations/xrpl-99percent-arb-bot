<!-- GSD:project-start source:PROJECT.md -->
## Project

**XRPL 99%+ Arbitrage Bot**

A deterministic XRPL arbitrage bot targeting 99%+ win rate on every executed trade. It uses hybrid AMM + order-book (CLOB) pathfinding via `ripple_path_find`, live ledger simulation via the `simulate` RPC, and a math-first approach where trades only fire when net profit exceeds 0.6% after fees and slippage. Built for a US resident, self-custody, on-chain DEX arbitrage — fully legal, no KYC. Deployable on a cheap Linux VPS (Hostinger KVM 1, 1 CPU, 4GB RAM) alongside an existing OpenClaw Docker project.

**Core Value:** Every executed trade must be mathematically near-certain profitable — the bot never submits a transaction that hasn't passed live ledger simulation with profit above threshold. Safety over speed, always.

### Constraints

- **VPS resources**: 1 CPU core, 4GB RAM — bot must be lightweight, no heavy ML or concurrent processes
- **Safety-first**: DRY_RUN=True for minimum 7 days before any live trading
- **Financial math**: All monetary calculations use `decimal.Decimal` — no floating point
- **Isolation**: Bot runs as `xrplbot` user, separate from root and OpenClaw Docker
- **Dependencies**: Minimal — only xrpl-py, python-dotenv, requests, anthropic, streamlit, pandas, plotly
- **No secrets in code**: All credentials via `.env` file, never committed
<!-- GSD:project-end -->

<!-- GSD:stack-start source:STACK.md -->
## Technology Stack

Technology stack not yet documented. Will populate after codebase mapping or first phase.
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, or `.github/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
