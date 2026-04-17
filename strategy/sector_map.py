"""Static sector classification for the wheel universe.

Used by the sector-exposure filter to prevent over-concentration in one
group (e.g. 3 MegaCap-tech shorts all get assigned on the same FOMC day).

Keep this module dependency-free so settings.py and wheel_filters.py can
import it without pulling in Alpaca.
"""
from __future__ import annotations

# Coarse classification chosen to match how our positions actually correlate,
# not GICS. "Hyperscaler" is its own bucket because MSFT/GOOGL/AMZN/META all
# move together on AI capex news. "Semi" includes anything AI-infra (not just
# chips — photonics, servers, memory ETF).
SECTOR_MAP: dict[str, str] = {
    # Hyperscaler / mega-cap tech
    "AAPL": "megacap_tech", "MSFT": "megacap_tech", "GOOGL": "megacap_tech",
    "META": "megacap_tech", "AMZN": "megacap_tech", "NFLX": "megacap_tech",

    # EV / Musk beta
    "TSLA": "ev_musk",

    # AI hardware / semis / AI-infra ETF
    "NVDA": "ai_hardware", "AMD": "ai_hardware", "AVGO": "ai_hardware",
    "TSM": "ai_hardware", "MU": "ai_hardware", "QCOM": "ai_hardware",
    "INTC": "ai_hardware", "AMAT": "ai_hardware", "ARM": "ai_hardware",
    "MRVL": "ai_hardware", "LITE": "ai_hardware", "COHR": "ai_hardware",
    "IPGP": "ai_hardware", "SMCI": "ai_hardware", "DRAM": "ai_hardware",

    # Software / enterprise
    "ORCL": "software", "CRM": "software", "ADBE": "software",
    "PLTR": "software",

    # High-volatility retail / crypto / meme
    "HOOD": "retail_crypto", "COIN": "retail_crypto", "MSTR": "retail_crypto",

    # Defensive
    "COST": "defensive", "UNH": "defensive", "XYL": "defensive",
}


def sector_of(symbol: str) -> str:
    """Return this symbol's sector bucket, or 'other' if unknown."""
    return SECTOR_MAP.get(symbol.upper(), "other")
