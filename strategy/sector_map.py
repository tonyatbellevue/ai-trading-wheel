"""Static sector classification for the wheel universe.

Used by the sector-exposure filter to prevent over-concentration in one
group (e.g. 3 MegaCap-tech shorts all get assigned on the same FOMC day).

Keep this module dependency-free so settings.py and wheel_filters.py can
import it without pulling in Alpaca.

v7 split (2026-05-07): ai_hardware (15 names) was too coarse — DRAM/NAND
spot prices and CPU x86 demand are largely independent drivers, but the
old bucket let one cap them together. Now:

  cpu             — INTC/ARM/QCOM      (x86 + ARM IP + mobile SoC)
  memory          — MU/DRAM/WDC/STX/SNDK (DRAM/NAND/HDD storage)
  ai_hardware     — NVDA/AMD/AVGO/TSM/SMCI (GPU + foundry + AI servers,
                    AI-capex driven)
  photonics       — MRVL/LITE/COHR/IPGP (光通讯: 800G/1.6T 光模块 +
                    硅光子 + 工业激光 + CPO. Driven by AI datacenter
                    interconnect demand but with industrial-laser tail
                    diversification — IPGP has 30%+ non-DC revenue)
  semi_equipment  — AMAT/LRCX/TER/ASML/KLAC (wafer fab equipment + test)
  analog          — NXPI/ADI/MCHP/ON   (analog/auto/IoT/embedded — these
                    correlate with auto + industrial cycles, NOT AI capex)

Note on AMD: kept in ai_hardware (not cpu) because >50% revenue / >70%
growth comes from datacenter GPU/MI300 — it correlates more with NVDA
than INTC nowadays.

Note on too-expensive memory/equipment names (WDC/STX/SNDK/ASML/KLAC):
their stock prices put them above the 40% single-position cap on a
$101k account, so kelly_contracts returns 0. Sector classification
still matters because if any are ever held (e.g. via assignment from
a different scenario or a future larger account), the sector cap kicks
in correctly.
"""
from __future__ import annotations

SECTOR_MAP: dict[str, str] = {
    # Hyperscaler / mega-cap tech
    "AAPL": "megacap_tech", "MSFT": "megacap_tech", "GOOGL": "megacap_tech",
    "META": "megacap_tech", "AMZN": "megacap_tech", "NFLX": "megacap_tech",

    # EV / Musk beta
    "TSLA": "ev_musk",

    # CPU — x86, ARM IP, mobile SoC (NOT AMD; see docstring)
    "INTC": "cpu", "ARM": "cpu", "QCOM": "cpu",

    # Memory / storage — DRAM/NAND spot-price + HDD demand driven
    "MU": "memory", "DRAM": "memory",
    "WDC": "memory", "STX": "memory", "SNDK": "memory",

    # AI hardware — GPU / foundry / AI servers / AI vision (AI-capex driven)
    "NVDA": "ai_hardware", "AMD": "ai_hardware", "AVGO": "ai_hardware",
    "TSM": "ai_hardware", "SMCI": "ai_hardware",
    "AMBA": "ai_hardware",  # AI 视觉处理器, edge inference

    # 光通讯 / Photonics — 光模块 + 硅光子 + 光网络设备 + 工业激光
    # Moved from ai_hardware 5/7. Driven by AI datacenter interconnect
    # demand but IPGP has industrial-laser tail diversification.
    "MRVL": "photonics", "LITE": "photonics",
    "COHR": "photonics", "IPGP": "photonics",
    "CIEN": "photonics",   # 光网络长距设备
    "FN": "photonics",     # 光学 EMS, supply chain to LITE/COHR/CIEN

    # 数据中心网络 — Ethernet 交换机 + 老牌网络设备
    # Different from photonics: this is electrical/L2-L7 networking,
    # not optical interconnect. Drivers: enterprise IT spending +
    # cloud DC buildout. Less AI-capex sensitive than photonics.
    "ANET": "networking",  # Arista — DC switches 龙头
    "CSCO": "networking",  # Cisco — 老牌, 安全 + 协议 + 服务

    # Semi equipment — wafer fab tools + test (correlate with capex cycle)
    "AMAT": "semi_equipment", "LRCX": "semi_equipment",
    "TER": "semi_equipment", "ASML": "semi_equipment", "KLAC": "semi_equipment",

    # Analog / auto / IoT / embedded — auto + industrial cycles, NOT AI capex
    "NXPI": "analog", "ADI": "analog",
    "MCHP": "analog", "ON": "analog",

    # Software / enterprise
    "ORCL": "software", "CRM": "software", "ADBE": "software",
    "PLTR": "software",

    # (retail_crypto sector removed 5/7 — HOOD/COIN/MSTR dropped from
    # universe per user request: too volatile, premium-thin-vs-risk
    # tradeoff was poor for this strategy.)

    # Defensive
    "COST": "defensive", "UNH": "defensive", "XYL": "defensive",

    # Broad-market index ETFs — different from single-stock risk profile
    # because they hold 100+ underlyings each. SPY ≈ 60% AI-related now;
    # QQQ ≈ 70% AI-related. Use is mainly for IV-rank arbitrage when
    # individual stocks are calm but the index has volatility.
    "SPY": "index_etf", "QQQ": "index_etf",

    # Aerospace / defense (5/27 added — SpaceX IPO 6/12 concept play)
    # Sector cap MAX_SECTOR_EXPOSURE_PCT=60% applies to total of these 3
    # so we don't accidentally over-concentrate on the SpaceX hype trade.
    "RKLB": "aerospace_defense",  # Rocket Lab — closest SpaceX proxy
    "LMT":  "aerospace_defense",  # Lockheed Martin — defense+space giant
    "LHX":  "aerospace_defense",  # L3Harris — space tech + defense

    # Financials (5/27 added — fill gap, hedge tech downturn)
    "BAC": "financials",   # Bank of America
    "JPM": "financials",   # JPMorgan

    # Energy (5/27 added — inflation hedge, anti-correlated to tech in down markets)
    "XOM": "energy",       # ExxonMobil

    # Pharma (5/27 added — defensive, anti-correlated to tech)
    "PFE": "pharma",       # Pfizer
    "JNJ": "pharma",       # Johnson & Johnson

    # Consumer staples (5/27 added — defensive)
    "WMT": "consumer_staples",  # Walmart

    # Auto (5/27 added — keeps TSLA isolated in ev_musk; F/GM together
    # for traditional auto sector beta)
    "F":  "auto",          # Ford
    "GM": "auto",          # General Motors
}


def sector_of(symbol: str) -> str:
    """Return this symbol's sector bucket, or 'other' if unknown."""
    return SECTOR_MAP.get(symbol.upper(), "other")
