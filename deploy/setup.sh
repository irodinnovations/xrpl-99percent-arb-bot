#!/usr/bin/env bash
# Run as: sudo bash deploy/setup.sh
#
# One-shot provisioning script for XRPL 99%+ Arbitrage Bot on Hostinger KVM 1
# (Ubuntu 25.10, 1 CPU core, 4GB RAM).
#
# OpenClaw Docker coexistence (DEP-05):
#   The bot is a PURE OUTBOUND WebSocket client — it binds to NO network ports.
#   OpenClaw Docker uses its own bridge network. There is ZERO port conflict risk.
#   The only shared resources are CPU and RAM, which are hard-capped by the
#   service unit file (CPUQuota=80%, MemoryMax=512M). OpenClaw retains the rest.
#
# Idempotent: safe to re-run — all steps check state before acting.

set -euo pipefail

echo "======================================================"
echo "  XRPL Arbitrage Bot — VPS Provisioning Setup"
echo "======================================================"
echo ""

# -------------------------------------------------------
# Step 1: Create xrplbot system user (if not already exists)
# -------------------------------------------------------
echo "[1/7] Creating xrplbot system user..."
if id -u xrplbot &>/dev/null; then
    echo "      xrplbot user already exists — skipping."
else
    useradd --system --no-create-home --home-dir /opt/xrplbot --shell /usr/sbin/nologin xrplbot
    echo "      xrplbot user created (no shell, no SSH access)."
fi

# -------------------------------------------------------
# Step 2: Create /opt/xrplbot directory
# -------------------------------------------------------
echo "[2/7] Creating /opt/xrplbot directory..."
mkdir -p /opt/xrplbot
echo "      Directory ready."

# -------------------------------------------------------
# Step 3: Remind user to copy code and .env
# -------------------------------------------------------
echo ""
echo "======================================================"
echo "  ACTION REQUIRED:"
echo "  Copy your project files to /opt/xrplbot/ and create"
echo "  /opt/xrplbot/.env before continuing."
echo ""
echo "  Minimum .env contents:"
echo "    XRPL_SECRET=your_wallet_seed"
echo "    XRPL_WS_URL=wss://s1.ripple.com"
echo "    XRPL_RPC_URL=https://s1.ripple.com"
echo "    DRY_RUN=True"
echo ""
echo "  Recommended transfer method:"
echo "    scp -r /local/path/xrpl-99percent-arb-bot/* root@<vps-ip>:/opt/xrplbot/"
echo "======================================================"
echo ""

# -------------------------------------------------------
# Step 4: Prompt to continue (idempotent pause)
# -------------------------------------------------------
read -p "Press Enter once files and .env are in place..."
echo ""

# -------------------------------------------------------
# Step 5: Set ownership and secure .env
# -------------------------------------------------------
echo "[5/7] Setting ownership and securing .env..."
chown -R xrplbot:xrplbot /opt/xrplbot
chmod 600 /opt/xrplbot/.env
echo "      Ownership set to xrplbot:xrplbot."
echo "      .env permissions set to 600 (T-04-02)."

# -------------------------------------------------------
# Step 6: Create Python virtual environment as xrplbot user
# -------------------------------------------------------
echo "[6/7] Creating Python virtual environment..."
# Check if venv already exists
if [ -f /opt/xrplbot/venv/bin/python ]; then
    echo "      Virtual environment already exists — reinstalling packages."
else
    sudo -u xrplbot python3 -m venv /opt/xrplbot/venv
    echo "      Virtual environment created."
fi
sudo -u xrplbot /opt/xrplbot/venv/bin/pip install --upgrade pip --quiet
sudo -u xrplbot /opt/xrplbot/venv/bin/pip install -r /opt/xrplbot/requirements.txt --quiet
echo "      Dependencies installed."

# -------------------------------------------------------
# Step 7: Install and enable the systemd service
# -------------------------------------------------------
echo "[7/7] Installing and enabling systemd service..."
cp /opt/xrplbot/deploy/xrplbot.service /etc/systemd/system/xrplbot.service
systemctl daemon-reload
systemctl enable xrplbot
echo "      Service installed and enabled."

# -------------------------------------------------------
# Done — print next steps
# -------------------------------------------------------
echo ""
echo "======================================================"
echo "  Setup complete!"
echo "======================================================"
echo ""
echo "Verify your .env has DRY_RUN=True, then start the bot:"
echo "  sudo systemctl start xrplbot"
echo "  sudo journalctl -u xrplbot -f"
echo ""
echo "To check status at any time:"
echo "  sudo systemctl status xrplbot"
echo ""
echo "OpenClaw Docker coexistence check:"
echo "  docker ps    <- confirm OpenClaw containers still running"
echo "  free -h      <- confirm memory available"
echo "  top          <- confirm CPU usage is within limits"
echo ""
echo "REMINDER: Run in DRY_RUN=True mode for at least 7 days"
echo "before switching to live trading."
echo ""
