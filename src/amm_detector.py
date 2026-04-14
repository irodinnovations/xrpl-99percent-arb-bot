"""AMM event detector — filters transaction stream for large AMM operations.

Large AMM deposits/withdrawals shift pool reserve ratios, temporarily
mispricing the AMM relative to CLOB.  Detecting these events triggers an
immediate scan cycle to capture the mispricing before other bots.

Listens to the transactions stream (via connection.on_transaction callback)
and filters for AMMDeposit, AMMWithdraw, and AMMBid transaction types that
move at least AMM_MIN_IMPACT_XRP worth of assets.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.config import AMM_MIN_IMPACT_XRP

logger = logging.getLogger(__name__)

DROPS_PER_XRP = Decimal("1000000")

# AMM transaction types that affect pool reserves
_AMM_TX_TYPES = frozenset({
    "AMMDeposit",
    "AMMWithdraw",
    "AMMBid",
    "AMMCreate",
    "AMMDelete",
})


@dataclass
class AMMEvent:
    """A significant AMM event detected from the transaction stream."""

    tx_type: str
    currency: str
    issuer: str
    xrp_amount: Decimal
    tx_hash: str


class AMMEventDetector:
    """Filters transaction stream messages for significant AMM operations."""

    def __init__(self, min_xrp_impact: Decimal = AMM_MIN_IMPACT_XRP):
        self._min_impact = min_xrp_impact

    def check_transaction(self, tx_msg: dict) -> Optional[AMMEvent]:
        """Check if a transaction message represents a significant AMM event.

        Args:
            tx_msg: A raw transaction message from the transactions stream.
                    Expected to have a "transaction" key with the tx fields.

        Returns:
            An AMMEvent if the transaction is a significant AMM operation,
            None otherwise.
        """
        try:
            tx = tx_msg.get("transaction", {})
            if not isinstance(tx, dict):
                return None

            tx_type = tx.get("TransactionType", "")
            if tx_type not in _AMM_TX_TYPES:
                return None

            # Only process validated (successful) transactions
            meta = tx_msg.get("meta", {})
            if isinstance(meta, dict):
                result = meta.get("TransactionResult", "")
                if result and result != "tesSUCCESS":
                    return None

            tx_hash = tx.get("hash", "unknown")

            # Extract XRP amount from the transaction
            xrp_amount = self._extract_xrp_amount(tx, meta)
            if xrp_amount < self._min_impact:
                return None

            # Extract the affected non-XRP currency
            currency, issuer = self._extract_currency(tx)
            if not currency:
                return None

            logger.info(
                f"AMM event: {tx_type} | {currency}/{issuer[:8]}... | "
                f"{xrp_amount:.2f} XRP | hash={tx_hash[:16]}..."
            )

            return AMMEvent(
                tx_type=tx_type,
                currency=currency,
                issuer=issuer,
                xrp_amount=xrp_amount,
                tx_hash=tx_hash,
            )

        except (TypeError, KeyError, AttributeError) as e:
            logger.debug(f"AMM event parse error: {e}")
            return None

    def _extract_xrp_amount(self, tx: dict, meta: dict) -> Decimal:
        """Estimate the XRP impact of an AMM transaction.

        Checks Amount, Amount2, and metadata AffectedNodes for XRP changes.
        """
        xrp_total = Decimal("0")

        for field in ("Amount", "Amount2", "LPTokenIn", "LPTokenOut"):
            value = tx.get(field)
            if value is None:
                continue
            # XRP amounts are strings of drops (not dicts)
            if isinstance(value, str):
                try:
                    xrp_total += Decimal(value) / DROPS_PER_XRP
                except InvalidOperation:
                    continue

        # If we couldn't find XRP in the tx fields, check metadata
        # for balance changes on AMM account
        if xrp_total == Decimal("0") and isinstance(meta, dict):
            for node in meta.get("AffectedNodes", []):
                modified = node.get("ModifiedNode", {})
                if modified.get("LedgerEntryType") == "AccountRoot":
                    prev = modified.get("PreviousFields", {})
                    final = modified.get("FinalFields", {})
                    if "Balance" in prev and "Balance" in final:
                        try:
                            diff = abs(
                                Decimal(final["Balance"]) - Decimal(prev["Balance"])
                            ) / DROPS_PER_XRP
                            xrp_total = max(xrp_total, diff)
                        except InvalidOperation:
                            continue

        return abs(xrp_total)

    @staticmethod
    def _extract_currency(tx: dict) -> tuple[str, str]:
        """Extract the non-XRP currency and issuer from an AMM transaction.

        Checks Asset and Asset2 fields (AMM pool identifiers).
        """
        for field in ("Asset", "Asset2", "Amount", "Amount2"):
            value = tx.get(field)
            if isinstance(value, dict):
                currency = value.get("currency", "")
                issuer = value.get("issuer", "")
                if currency and issuer:
                    return currency, issuer

        return "", ""
