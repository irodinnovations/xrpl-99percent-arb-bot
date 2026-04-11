"""XRPL 99%+ Arbitrage Bot — main entry point.

Connects to XRPL mainnet, scans for arbitrage opportunities each ledger close,
validates via simulate RPC, and either paper-trades or live-executes.

Safety rules enforced here:
- Bot never starts without XRPL_SECRET set (T-01-10)
- DRY_RUN defaults to True in config — explicit .env change required (DRY-04)
- Top-level exception handler in on_ledger_close prevents main loop crash (T-01-11)
- Circuit breaker checked every scan cycle — halts on daily loss limit (SAFE-02)
"""

import asyncio
import logging
from decimal import Decimal

from xrpl.wallet import Wallet

from src.config import (
    XRPL_SECRET,
    DRY_RUN,
    LOG_LEVEL,
    PROFIT_THRESHOLD,
    MAX_POSITION_PCT,
)
from src.connection import XRPLConnection
from src.pathfinder import PathFinder
from src.executor import TradeExecutor
from src.safety import CircuitBreaker, Blacklist
from src.trade_logger import setup_logging
from src.telegram_alerts import send_alert
from src.ai_brain import review_trade

logger = logging.getLogger(__name__)


async def main():
    """Main bot loop.

    Initializes all modules, registers the ledger-close callback, and
    connects to XRPL. The connection loop runs forever with auto-reconnect.
    """
    # Setup logging first so all subsequent logs are formatted
    setup_logging()

    # Validate wallet secret before doing anything else (T-01-10)
    if not XRPL_SECRET:
        logger.error("XRPL_SECRET not set in .env — cannot start bot")
        return

    wallet = Wallet.from_seed(XRPL_SECRET)
    logger.info(f"Wallet address: {wallet.address}")
    logger.info(f"Mode: {'DRY RUN (paper trading)' if DRY_RUN else 'LIVE TRADING'}")
    logger.info(f"Profit threshold: {PROFIT_THRESHOLD * 100}%")
    logger.info(f"Max position: {MAX_POSITION_PCT * 100}% of balance")

    # Initialize all modules
    connection = XRPLConnection()
    pathfinder = PathFinder(connection, wallet.address)
    circuit_breaker = CircuitBreaker(
        account_address=wallet.address,
        connection=connection,
    )
    blacklist = Blacklist()
    executor = TradeExecutor(
        wallet=wallet,
        circuit_breaker=circuit_breaker,
        blacklist=blacklist,
        dry_run=DRY_RUN,
    )

    # Track scan count for heartbeat logging
    scan_count = 0

    async def on_ledger_close(ledger_index: int):
        """Called every ~3-5 seconds on each XRPL ledger close.

        Top-level try/except prevents any single scan error from crashing
        the bot — connection auto-reconnects independently (T-01-11).
        """
        nonlocal scan_count
        scan_count += 1

        # Skip scans while circuit breaker is active; log every ~5 minutes
        if circuit_breaker.is_halted():
            if scan_count % 100 == 0:
                logger.info("Circuit breaker active — skipping scans")
            return

        try:
            # Get current balance for position sizing
            balance = await connection.get_account_balance(wallet.address)
            if balance <= Decimal("0"):
                logger.warning("Zero balance — skipping scan")
                return

            # Set circuit breaker reference balance on first successful fetch (SAFE-02)
            if circuit_breaker.reference_balance <= Decimal("0"):
                circuit_breaker.reference_balance = balance
                logger.info(f"Circuit breaker reference balance set: {balance} XRP")

            # Scan for arbitrage opportunities via ripple_path_find
            opportunities = await pathfinder.scan(balance)

            for opp in opportunities:
                logger.info(
                    f"Opportunity: {opp.profit_pct:.4f}% profit | "
                    f"In: {opp.input_xrp} XRP -> Out: {opp.output_xrp} XRP"
                )
                result = await executor.execute(opp)
                if result:
                    # Fire-and-forget AI review — never blocks scanning (AI-01)
                    trade_review_data = {
                        "profit_pct": str(opp.profit_pct),
                        "profit_ratio": str(opp.profit_ratio),
                        "input_xrp": str(opp.input_xrp),
                        "output_xrp": str(opp.output_xrp),
                        "dry_run": DRY_RUN,
                    }
                    asyncio.create_task(review_trade(trade_review_data))

            # Heartbeat log every ~50 ledgers (~3 minutes at 3-5s per ledger)
            if scan_count % 50 == 0:
                logger.info(
                    f"Heartbeat: ledger={ledger_index}, scans={scan_count}, "
                    f"balance={balance} XRP, halted={circuit_breaker.is_halted()}"
                )

        except Exception as e:
            # Log error but never re-raise — bot must keep scanning (T-01-11)
            logger.error(f"Scan error at ledger {ledger_index}: {e}")

    # Register the ledger-close callback before connecting
    connection.on_ledger_close(on_ledger_close)

    # Send startup alert (gracefully skipped if Telegram not configured)
    await send_alert(
        f"Bot started | Mode: {'DRY RUN' if DRY_RUN else 'LIVE'} | "
        f"Address: {wallet.address}"
    )

    logger.info("Starting XRPL connection...")
    # connect() runs forever — auto-reconnects on disconnect
    await connection.connect()


if __name__ == "__main__":
    asyncio.run(main())
