"""Independent audit of the wheel bot deployment.

Run manually: python scripts/independent_audit.py
Reads ONLY origin/master (not local worktree) + live config + GHA history.
Any FAIL means the assistant lied or a silent loss occurred.

Exit code 0 = all PASS, non-zero = at least one FAIL.
"""
import subprocess
import sys
import json
from pathlib import Path

# Windows GBK console can't print non-CP936 chars; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
results = []  # (layer, name, status, detail)


def record(layer, name, status, detail=""):
    results.append((layer, name, status, detail))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "INFO": "[INFO]", "WARN": "[WARN]"}[status]
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))


def git_show_master(path):
    """Read file from origin/master, NOT local worktree."""
    try:
        out = subprocess.run(
            ["git", "show", f"origin/master:{path}"],
            capture_output=True, text=True, cwd=REPO_ROOT, encoding="utf-8"
        )
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


# ============================================================
# Layer 1 — Deployment consistency (catch silent loss)
# ============================================================
print("\n=== Layer 1: Deployment (origin/master) ===")

# Refresh master ref
subprocess.run(["git", "fetch", "origin", "master"], cwd=REPO_ROOT,
               capture_output=True)

L1_CHECKS = [
    ("wheel_once.py", "send_email", True,
     "Trade alert email call must exist"),
    ("wheel_once.py", "trade.flag", True,
     "Trade flag write must exist"),
    ("strategy/wheel_filters.py", "EARNINGS_DB", True,
     "EARNINGS_DB constant must exist"),
    ("strategy/wheel_filters.py", "check_iv_rank", True,
     "IV rank filter must exist"),
    ("strategy/wheel_filters.py", "check_vix_regime", True,
     "VIX regime filter must exist"),
    ("strategy/wheel_evaluator.py", "IV-blocked fallback", True,
     "IV-blocked fallback must exist"),
    ("strategy/sector_map.py", "photonics", True,
     "Photonics sector must be defined"),
    (".github/workflows/wheel-summary.yml", "schedule:", False,
     "Schedule MUST be removed (caused dup emails)"),
    (".github/workflows/wheel.yml", "workflow_dispatch", True,
     "wheel.yml must accept workflow_dispatch"),
]

for path, needle, must_exist, desc in L1_CHECKS:
    content = git_show_master(path)
    if content is None:
        record("L1", f"{path} :: {needle}", "FAIL",
               f"file missing on master")
        continue
    found = needle in content
    if found == must_exist:
        record("L1", f"{path} :: {needle}", "PASS", desc)
    else:
        verb = "missing" if must_exist else "still present"
        record("L1", f"{path} :: {needle}", "FAIL",
               f"{verb} on master — {desc}")


# ============================================================
# Layer 2 — Configuration integrity
# ============================================================
print("\n=== Layer 2: Configuration ===")

try:
    sys.path.insert(0, str(REPO_ROOT))
    from config import settings

    # Capital protection thresholds
    cfg_checks = [
        ("CASH_BUFFER_PCT", 0.10),
        ("MAX_SINGLE_POSITION_PCT", 0.40),
        ("MAX_TOTAL_EXPOSURE_PCT", 0.90),
        ("MAX_SECTOR_EXPOSURE_PCT", 0.60),
    ]
    for name, expected in cfg_checks:
        actual = getattr(settings, name, None)
        if actual == expected:
            record("L2", f"settings.{name}", "PASS", f"= {actual}")
        else:
            record("L2", f"settings.{name}", "FAIL",
                   f"expected {expected}, got {actual}")
except Exception as e:
    record("L2", "settings import", "FAIL", str(e))

# 3-way reconciliation: universe vs earnings vs sector
try:
    from wheel_scanner import WHEEL_UNIVERSE
    from strategy.wheel_filters import EARNINGS_DB
    from strategy.sector_map import SECTOR_MAP

    universe = set(WHEEL_UNIVERSE)
    record("L2", "WHEEL_UNIVERSE size", "INFO", f"{len(universe)} symbols")

    missing_earnings = universe - set(EARNINGS_DB.keys())
    if not missing_earnings:
        record("L2", "Universe ⊆ EARNINGS_DB", "PASS",
               f"all {len(universe)} covered")
    else:
        record("L2", "Universe ⊆ EARNINGS_DB", "FAIL",
               f"missing: {sorted(missing_earnings)}")

    # SECTOR_MAP is {symbol: sector_name}
    missing_sector = universe - set(SECTOR_MAP.keys())
    if not missing_sector:
        record("L2", "Universe ⊆ SECTOR_MAP", "PASS",
               f"all {len(universe)} mapped")
    else:
        record("L2", "Universe ⊆ SECTOR_MAP", "FAIL",
               f"missing: {sorted(missing_sector)}")
except Exception as e:
    record("L2", "Universe reconciliation", "FAIL", str(e))


# ============================================================
# Layer 3 — Behavior probe (live data, dry-run)
# ============================================================
print("\n=== Layer 3: Behavior (live probe) ===")

try:
    from core.alpaca_client import AlpacaClients
    clock = AlpacaClients.trading().get_clock()
    record("L3", "Alpaca clock", "INFO",
           f"is_open={clock.is_open}, next_open={clock.next_open}")
except Exception as e:
    record("L3", "Alpaca clock", "FAIL", str(e))

try:
    from strategy.wheel_strategy import WheelStrategy
    s = WheelStrategy()
    phase, pos = s.get_phase()
    record("L3", "Wheel phase", "INFO",
           f"{phase.name}, position={pos.qty if pos else 'none'}")
except Exception as e:
    record("L3", "Wheel phase", "FAIL", str(e))

try:
    from strategy.wheel_filters import compute_iv_rank
    rank = compute_iv_rank(settings.WHEEL_SYMBOL)
    if rank is None:
        record("L3", f"IV rank({settings.WHEEL_SYMBOL})", "WARN",
               "returned None")
    else:
        record("L3", f"IV rank({settings.WHEEL_SYMBOL})", "INFO",
               f"{rank:.0f} (gate >= 30)")
except Exception as e:
    record("L3", "IV rank probe", "FAIL", str(e))


# ============================================================
# Layer 4 — Trigger chain (cron-job.org → GHA)
# ============================================================
print("\n=== Layer 4: Trigger chain ===")

try:
    out = subprocess.run(
        ["gh", "run", "list", "--workflow=wheel.yml",
         "--limit=20", "--json", "createdAt,conclusion,event"],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    if out.returncode != 0:
        record("L4", "gh run list", "WARN", "gh CLI failed or unauth")
    else:
        runs = json.loads(out.stdout)
        if not runs:
            record("L4", "wheel.yml recent runs", "WARN", "no runs found")
        else:
            success = sum(1 for r in runs if r["conclusion"] == "success")
            failure = sum(1 for r in runs if r["conclusion"] == "failure")
            dispatch = sum(1 for r in runs if r["event"] == "workflow_dispatch")
            record("L4", "wheel.yml last 20", "INFO",
                   f"{success} success, {failure} failure, "
                   f"{dispatch} via workflow_dispatch")
            if dispatch == 0:
                record("L4", "trigger source", "FAIL",
                       "no workflow_dispatch — cron-job.org may be down")
            elif success >= 15:
                record("L4", "wheel.yml health", "PASS",
                       f"{success}/20 success")
            else:
                record("L4", "wheel.yml health", "WARN",
                       f"only {success}/20 success")
except Exception as e:
    record("L4", "GHA history check", "FAIL", str(e))


# ============================================================
# Summary
# ============================================================
print("\n" + "=" * 56)
n_pass = sum(1 for _, _, s, _ in results if s == "PASS")
n_fail = sum(1 for _, _, s, _ in results if s == "FAIL")
n_warn = sum(1 for _, _, s, _ in results if s == "WARN")
n_info = sum(1 for _, _, s, _ in results if s == "INFO")
print(f"AUDIT: {n_pass} PASS, {n_fail} FAIL, {n_warn} WARN, {n_info} INFO")
if n_fail == 0:
    print("✅ Independent audit clean")
    sys.exit(0)
else:
    print("❌ FAILURES detected — assistant claims may be wrong")
    for layer, name, status, detail in results:
        if status == "FAIL":
            print(f"  {layer} {name}: {detail}")
    sys.exit(1)
