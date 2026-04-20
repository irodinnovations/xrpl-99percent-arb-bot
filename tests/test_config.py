"""Static checks on src.config and .env.example — proves CLEAN-01, CURR-01, CURR-03."""
from pathlib import Path
import re
import importlib


def test_leg2_timeout_ledgers_exists_and_defaults_to_four():
    from src.config import LEG2_TIMEOUT_LEDGERS
    assert LEG2_TIMEOUT_LEDGERS == 4
    assert isinstance(LEG2_TIMEOUT_LEDGERS, int)
    assert not isinstance(LEG2_TIMEOUT_LEDGERS, bool)


def test_env_example_documents_leg2_timeout_ledgers():
    env_text = Path(".env.example").read_text(encoding="utf-8")
    assert "LEG2_TIMEOUT_LEDGERS=4" in env_text
    # Require an explanatory comment near the key
    idx = env_text.find("LEG2_TIMEOUT_LEDGERS=")
    preceding = env_text[:idx]
    # Last 500 chars of preceding must mention atomic-submit semantics
    context_block = preceding[-500:].lower()
    assert any(
        marker in context_block
        for marker in ("atomic", "two-leg", "both legs")
    ), "LEG2_TIMEOUT_LEDGERS must have an explanatory comment mentioning atomic/two-leg semantics"


def test_high_liq_default_includes_solo_and_usdt():
    import src.config as config
    importlib.reload(config)
    assert "SOLO" in config.HIGH_LIQ_CURRENCIES
    assert "USDT" in config.HIGH_LIQ_CURRENCIES
    assert len(config.HIGH_LIQ_CURRENCIES) >= 6


def test_high_liq_default_preserves_legacy_four():
    import src.config as config
    importlib.reload(config)
    for legacy in ("USD", "USDC", "RLUSD", "EUR"):
        assert legacy in config.HIGH_LIQ_CURRENCIES


def test_env_example_documents_every_high_liq_issuer():
    env_text = Path(".env.example").read_text(encoding="utf-8")
    # Must declare HIGH_LIQ_CURRENCIES with the new default
    assert "HIGH_LIQ_CURRENCIES=USD,USDC,RLUSD,EUR,SOLO,USDT" in env_text
    # Every HIGH_LIQ currency must appear near a valid r-address in the file
    r_addr_pattern = re.compile(r"r[1-9A-HJ-NP-Za-km-z]{24,34}")
    for currency in ("USD", "USDC", "RLUSD", "EUR", "SOLO", "USDT"):
        # Find lines mentioning the currency name (uppercase whole word)
        lines_with_currency = [
            line for line in env_text.splitlines()
            if re.search(rf"\b{currency}\b", line)
        ]
        # At least one of those lines must also contain an r-address
        assert any(r_addr_pattern.search(line) for line in lines_with_currency), (
            f"Currency {currency!r} has no documented r-address in .env.example"
        )


def test_high_liq_env_override_reloads(monkeypatch):
    """CURR-02: changing the env var and reloading the module updates the list."""
    monkeypatch.setenv("HIGH_LIQ_CURRENCIES", "USD,SOLO,BTC")
    import src.config as config
    importlib.reload(config)
    try:
        assert config.HIGH_LIQ_CURRENCIES == ["USD", "SOLO", "BTC"]
    finally:
        monkeypatch.delenv("HIGH_LIQ_CURRENCIES", raising=False)
        importlib.reload(config)
