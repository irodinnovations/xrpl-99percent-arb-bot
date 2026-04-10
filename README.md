# XRPL 99%+ Arbitrage Bot

A deterministic arbitrage bot for the XRP Ledger (XRPL) that targets a 99%+ win rate on every executed trade. It scans the live ledger for price inefficiencies, runs each candidate trade through a live ledger simulation before committing any funds, and only executes trades where net profit (after fees and slippage) exceeds 0.6%. The core principle is **safety over speed** - the bot never submits a real transaction it has not already proven profitable on a live dry-run.

---

## How It Works (Brief)

- **WebSocket stream** - connects to XRPL mainnet and listens for each ledger close (~3-5 seconds apart)
- **`ripple_path_find`** - asks the XRPL network to auto-route a trade across all AMM pools and order books simultaneously
- **Profit math** - calculates net profit after the 0.6% threshold, 0.3% slippage buffer, and network fee; rejects anything that does not clear the bar
- **`simulate` RPC gate** - sends the exact transaction to the XRPL as a dry-run on the live ledger; if it does not return `tesSUCCESS`, the trade is discarded
- **DRY_RUN log or live execute** - in paper mode, logs the would-be trade; in live mode, signs and submits the transaction
- **Telegram alert** - notifies you of every opportunity found (optional)

---

## Requirements

- Hostinger KVM 1 VPS or any Ubuntu 22.04+ Linux server with at least 1GB RAM
- Python 3.11 or newer
- An XRPL mainnet wallet with at least 10 XRP for live trading (0 XRP needed for paper trading)
- (Optional) Telegram bot token and chat ID for trade alerts
- (Optional) Anthropic API key for AI post-trade review (Claude)

---

## Deployment: Hostinger KVM 1

### 1. First-time VPS setup

SSH into your VPS as root and update the system:

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git
```

### 2. Clone the repository

```bash
cd /opt
git clone https://github.com/youruser/xrpl-99percent-arb-bot.git xrplbot
```

Alternatively, if you prefer not to use git on the VPS, transfer files with `scp`:

```bash
# Run from your local machine:
scp -r /local/path/xrpl-99percent-arb-bot/* root@YOUR_VPS_IP:/opt/xrplbot/
```

The only requirement is that all project files end up in `/opt/xrplbot/`.

### 3. Configure environment

```bash
cd /opt/xrplbot
cp .env.example .env
nano .env
```

Fill in `XRPL_SECRET` with your wallet seed. Leave everything else at the defaults for now.

**Important:** Keep `DRY_RUN=True` for at least 7 days. Do not enter a real wallet seed until you have completed the paper-trading checklist below. Never commit `.env` to git - it is already in `.gitignore`.

### 4. Run the setup script

```bash
sudo bash deploy/setup.sh
```

This script does the following (it is safe to re-run - all steps are idempotent):

- Creates the `xrplbot` system user (no shell, no SSH access)
- Creates `/opt/xrplbot` if it does not exist
- **Pauses** and prompts you to copy your files and create `.env` (skip if you already did this)
- Sets ownership (`chown -R xrplbot:xrplbot`) and secures `.env` (`chmod 600`)
- Creates a Python virtual environment as the `xrplbot` user
- Installs all dependencies from `requirements.txt`
- Copies `deploy/xrplbot.service` to `/etc/systemd/system/` and enables it

### 5. Start the bot

```bash
sudo systemctl start xrplbot
sudo systemctl status xrplbot
sudo journalctl -u xrplbot -f
```

You should see output like:

```
Mode: DRY RUN (paper trading)
Wallet address: rXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
Connecting to wss://s1.ripple.com ...
Heartbeat - ledger 12345678 | balance: 0.00 XRP
```

If you see `Mode: DRY RUN (paper trading)`, the bot is running correctly in safe paper mode.

### 6. Coexistence with OpenClaw Docker

The xrplbot service is designed to share a VPS with OpenClaw Docker without any conflicts:

- The bot is a **pure outbound WebSocket client** - it binds to no TCP or UDP ports, so there is zero port conflict risk with Docker
- OpenClaw Docker containers use their own bridge network - the bot cannot reach or disrupt them
- The bot is resource-capped in `deploy/xrplbot.service`: `CPUQuota=80%` and `MemoryMax=512M`, leaving the remaining CPU and ~3.5GB RAM for OpenClaw and the OS
- Swap is disabled for the bot (`MemorySwapMax=0`) to prevent latency spikes from page faults

Verify coexistence after starting the bot:

```bash
docker ps          # OpenClaw containers should still be running
free -h            # Memory should still show available headroom
htop               # CPU usage should be well below 80%
```

Both services are managed independently - `sudo systemctl stop xrplbot` has zero effect on Docker.

### 7. Starting the dashboard (optional)

The Streamlit dashboard shows win rate, profit histogram, and recent opportunities. It runs on the VPS and you access it via an SSH tunnel on your local machine.

**On the VPS:**

```bash
sudo -u xrplbot /opt/xrplbot/venv/bin/streamlit run /opt/xrplbot/src/dashboard.py --server.port 8501
```

**On your local machine (separate terminal):**

```bash
ssh -L 8501:localhost:8501 root@YOUR_VPS_IP
```

Then open your browser to: `http://localhost:8501`

---

## 7-Day Paper Trading Review Checklist

**Complete ALL of the following before switching to live trading.** Running 7 days of paper trading is not optional - it validates that the bot runs stably on your specific VPS configuration and that the profit math works for current market conditions.

- [ ] **Bot stays connected**: No more than 2-3 unexpected disconnects per day. Check with:
  ```bash
  journalctl -u xrplbot --since "7 days ago" | grep -c "Reconnecting"
  ```

- [ ] **Heartbeat logs present**: Bot logs a heartbeat every ~3 minutes. At least 2,800 heartbeats expected over 7 days. Check with:
  ```bash
  journalctl -u xrplbot --since "7 days ago" | grep -c "Heartbeat"
  ```

- [ ] **Opportunities detected**: Bot finds at least some opportunities (even if win rate is low at first). Check with:
  ```bash
  wc -l /opt/xrplbot/xrpl_arb_log.jsonl
  ```

- [ ] **Win rate > 90%**: Paper trade win rate should exceed 90% before going live. The dashboard shows current win rate. If below 90%, do NOT go live - increase `PROFIT_THRESHOLD` and continue paper trading.

- [ ] **Circuit breaker never triggered**: The daily loss circuit breaker should never have fired during paper trading. Check with:
  ```bash
  journalctl -u xrplbot --since "7 days ago" | grep "Circuit breaker"
  ```
  This should return nothing.

- [ ] **No unexpected Python exceptions**: Occasional network errors are fine; logic errors are not. Check with:
  ```bash
  journalctl -u xrplbot --since "7 days ago" | grep -c "ERROR"
  ```
  Result should be fewer than 10.

- [ ] **AI reviews logging** (if configured): Reviews should be accumulating in the file:
  ```bash
  wc -l /opt/xrplbot/ai_reviews.jsonl
  ```

- [ ] **Telegram alerts arriving** (if configured): You should have received at least one opportunity alert during the 7 days. If configured but no alerts arrived, the bot is not finding opportunities - investigate before going live.

- [ ] **Memory stable**: No out-of-memory kills. Check with:
  ```bash
  journalctl -u xrplbot --since "7 days ago" | grep "MemoryMax"
  ```
  This should return nothing.

- [ ] **You understand the logs**: You have read at least 20 raw log entries in `xrpl_arb_log.jsonl` and understand each field. Run `backtest.py` and review the win rate report.

**If any item is NOT checked: do not go live. Fix the issue first.**

---

## Switching to Live Trading

This is a deliberate process - not something done casually. Real XRP will be spent once `DRY_RUN=False`.

### Prerequisites

All 10 items in the 7-day checklist above must be checked off. No exceptions.

### Step 1: Fund your wallet

Send exactly **10-20 XRP** to your wallet address. The address is shown in the bot startup log as `Wallet address: rXXXXXX...`.

Verify the XRP arrived:
```
https://livenet.xrpl.org/accounts/YOUR_WALLET_ADDRESS
```

Do not send more than 20 XRP for your first live session. At `MAX_POSITION_PCT=0.05` (5%), the maximum trade size at 10 XRP is 0.5 XRP - intentionally small to limit first-session risk.

### Step 2: Update .env

```bash
nano /opt/xrplbot/.env
```

Change:
```
DRY_RUN=True
```
to:
```
DRY_RUN=False
```

Confirm `MAX_POSITION_PCT=0.05` (5%) is still set. Do not increase this for your first session.

### Step 3: Restart the bot

```bash
sudo systemctl restart xrplbot
sudo journalctl -u xrplbot -f
```

Confirm you see `Mode: LIVE TRADING` in the startup log. If you see `Mode: DRY RUN`, the `.env` change did not take effect - check the file and try again.

### Step 4: Monitor the first hour

- Watch `journalctl -u xrplbot -f` for at least the first 30 minutes
- The dashboard should update with live trades (entries with `dry_run: false`)
- If anything looks wrong, stop the bot immediately:
  ```bash
  sudo systemctl stop xrplbot
  ```

### Reverting to paper trading

At any time, edit `.env`, set `DRY_RUN=True`, and restart the service:

```bash
nano /opt/xrplbot/.env    # set DRY_RUN=True
sudo systemctl restart xrplbot
```

The bot returns to paper mode in under 30 seconds.

---

## Running the Backtester

The backtester reads your accumulated paper trade log and produces a win rate report:

```bash
sudo -u xrplbot /opt/xrplbot/venv/bin/python backtest.py
```

Run this at the end of your 7-day paper trading period to get a statistical baseline before going live.

---

## Useful Commands

| Task | Command |
|------|---------|
| Start bot | `sudo systemctl start xrplbot` |
| Stop bot | `sudo systemctl stop xrplbot` |
| Restart bot | `sudo systemctl restart xrplbot` |
| View live logs | `sudo journalctl -u xrplbot -f` |
| View last 100 lines | `sudo journalctl -u xrplbot -n 100` |
| Check service status | `sudo systemctl status xrplbot` |
| View trade log | `tail -f /opt/xrplbot/xrpl_arb_log.jsonl` |
| Count paper trades | `wc -l /opt/xrplbot/xrpl_arb_log.jsonl` |
| Check memory usage | `systemctl show xrplbot --property=MemoryCurrent` |

---

## Security Notes

- The bot runs as `xrplbot` - a no-login system user with no shell access. Even if someone gains SSH access to your VPS, they cannot log in as `xrplbot`.
- `.env` is readable only by `xrplbot` (`chmod 600`) - other users and processes on the VPS cannot read your wallet seed.
- The bot cannot escalate its own privileges (`NoNewPrivileges=true` in the service file).
- `XRPL_SECRET` is loaded via `EnvironmentFile` in the systemd service - it never appears in `ps aux` output or command-line process lists.
- The bot can only write to `/opt/xrplbot/` - it cannot modify system files (`ProtectSystem=strict` in the service file).
- Never commit `.env` to git. It is already listed in `.gitignore`, but double-check before any `git push`.

---

## Project Structure

```
xrpl-99percent-arb-bot/
├── main.py                  # Bot entry point - scanner loop, WebSocket client
├── backtest.py              # Backtester - reads trade log, outputs win rate report
├── requirements.txt         # Python dependencies
├── .env.example             # Template - copy to .env and fill in your values
├── src/
│   ├── config.py            # Environment variable loading (Decimal constants)
│   ├── connection.py        # XRPL WebSocket connection and reconnect logic
│   ├── pathfinder.py        # ripple_path_find - hybrid AMM + CLOB routing
│   ├── profit_math.py       # Net profit calculation with Decimal precision
│   ├── simulator.py         # simulate RPC - live ledger dry-run gate
│   ├── executor.py          # Transaction build, sign, and submit
│   ├── safety.py            # Circuit breaker, position size limits, blacklist
│   ├── trade_logger.py      # JSONL trade log writer
│   ├── telegram_alerts.py   # Optional Telegram notifications
│   ├── ai_brain.py          # Optional async Claude post-trade review
│   ├── backtester.py        # Core backtester logic (called by backtest.py)
│   └── dashboard.py         # Streamlit read-only dashboard
└── deploy/
    ├── xrplbot.service      # systemd unit file (copy to /etc/systemd/system/)
    └── setup.sh             # One-shot VPS provisioning script (idempotent)
```

---

## Disclaimer

This bot executes real financial transactions on the XRP Ledger. Trading involves risk - past paper-trade performance does not guarantee future live-trade results. Start with a small amount (10-20 XRP) and only increase capital after sustained live-trading success. The 7-day paper-trading requirement exists for good reason - please follow it.
