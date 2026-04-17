"""Wheel bot recovery — retry emails, run health check, optionally reset shadow."""
from __future__ import annotations
import sys, os, io, argparse, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from pathlib import Path
from loguru import logger
logger.remove()

from config import settings


def stage_retry_emails() -> int:
    from utils.emailer import FAILED_DIR, retry_failed_emails
    before = len(list(FAILED_DIR.glob("*.json")))
    if before == 0:
        print("## 1. Email queue")
        print("   (empty — nothing to retry)\n")
        return 0

    print(f"## 1. Email queue: retrying {before} failed email(s)...")
    result = retry_failed_emails(max_per_run=20)
    after = len(list(FAILED_DIR.glob("*.json")))
    print(f"   sent: {result['sent']}  |  still_failed: {result['still_failed']}")
    print(f"   remaining in queue: {after}\n")
    return result["still_failed"]


def stage_reset_shadow() -> None:
    print("## 2. Shadow reset")
    data_dir = Path(settings.BASE_DIR) / "metrics" / "data"
    targets = [
        data_dir / "shadow_state.json",
        data_dir / "shadow_decisions.csv",
        data_dir / "shadow_equity.csv",
        data_dir / "shadow_rotations.csv",
    ]
    removed = []
    for p in targets:
        if p.exists():
            p.unlink()
            removed.append(p.name)
    if removed:
        print(f"   removed: {', '.join(removed)}")
        print("   Next health check will recreate shadow from a fresh $100k virtual account.\n")
    else:
        print("   (no shadow files existed — clean already)\n")


def stage_health_check() -> int:
    print("## 3. Health check")
    script = Path(PROJECT_ROOT) / "scripts" / "health_check.py"
    result = subprocess.run(
        [sys.executable, str(script), "--no-email"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    # health_check.py writes its markdown table to stdout
    print(result.stdout)
    if result.returncode != 0:
        print(f"   ⚠ health_check exited with code {result.returncode}")
        if result.stderr:
            print(f"   stderr: {result.stderr.strip()[:500]}")
    return result.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-shadow", action="store_true",
                    help="Discard shadow account state + CSVs (4 weeks of tracking lost)")
    args = ap.parse_args()

    print("# Wheel bot recovery\n")
    still_failed = stage_retry_emails()
    if args.reset_shadow:
        stage_reset_shadow()
    hc_code = stage_health_check()

    # Summary
    print("## Summary")
    if still_failed == 0 and hc_code == 0:
        print("✓ All green.")
    else:
        if still_failed:
            print(f"⚠ {still_failed} email(s) still failing — likely SMTP auth issue. "
                  "Check EMAIL_PASSWORD in .env and Gmail app-password status.")
        if hc_code:
            print(f"⚠ Health check returned {hc_code}. See table above. "
                  "If `alpaca_trading` is failing, the Alpaca key rotated — refresh "
                  ".env AND `gh secret set ALPACA_API_KEY`.")
    return 0 if (still_failed == 0 and hc_code == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
