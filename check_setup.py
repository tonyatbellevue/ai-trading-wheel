"""环境自检 — 在跑 wheel_summary.py / wheel_once.py 前验证配置

用法：
    python check_setup.py

检查项：
    1. .env 文件存在吗？
    2. ALPACA_API_KEY / ALPACA_SECRET_KEY 都设了吗？
    3. 关键 Python 包都装了吗？
    4. 能不能连通 Alpaca API endpoint？
    5. 真 API key 能认证通过吗？（用 key 请求 /v2/account）

每个失败项都附带具体修复命令。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

OK = "\033[32m[✓]\033[0m"
FAIL = "\033[31m[✗]\033[0m"
WARN = "\033[33m[!]\033[0m"

failures: list[str] = []


def check(name: str, passed: bool, detail: str = "", fix: str = "") -> bool:
    icon = OK if passed else FAIL
    print(f"{icon} {name}")
    if detail:
        print(f"    {detail}")
    if not passed:
        if fix:
            print(f"    → 修复：{fix}")
        failures.append(name)
    return passed


def main() -> int:
    print("━" * 64)
    print("  Wheel 交易策略 — 环境自检")
    print("━" * 64)
    print()

    # ── 1. .env 文件 ──────────────────────────────────────────────
    check(
        ".env 文件存在",
        ENV_FILE.exists(),
        detail=f"路径: {ENV_FILE}",
        fix="cp .env.example .env  然后编辑填入 Alpaca key",
    )

    # ── 2. 加载 .env 并检查 key ───────────────────────────────────
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        print(f"{WARN} python-dotenv 未安装，跳过 .env 加载")

    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()

    key_ok = bool(api_key) and api_key not in ("PK_YOUR_KEY_ID_HERE", "DEMO_KEY")
    check(
        "ALPACA_API_KEY 已设置",
        key_ok,
        detail=f"值: {api_key[:6]}...（共 {len(api_key)} 字符）" if key_ok else "未设置或仍是占位符",
        fix="在 .env 里写：ALPACA_API_KEY=PK... （https://app.alpaca.markets/paper/dashboard/overview 生成）",
    )

    secret_ok = bool(secret_key) and secret_key not in ("YOUR_SECRET_KEY_HERE", "DEMO_SECRET")
    check(
        "ALPACA_SECRET_KEY 已设置",
        secret_ok,
        detail=f"长度: {len(secret_key)} 字符" if secret_ok else "未设置或仍是占位符",
        fix="在 .env 里写：ALPACA_SECRET_KEY=... （和 API Key 同一个弹窗里）",
    )

    # ── 3. Python 包 ──────────────────────────────────────────────
    required = {
        "alpaca-py": "alpaca",
        "pandas": "pandas",
        "loguru": "loguru",
        "python-dotenv": "dotenv",
        "pytz": "pytz",
        "pydantic": "pydantic",
        "requests": "requests",
    }
    missing = []
    for pkg_name, import_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg_name)

    check(
        "关键 Python 包已安装",
        not missing,
        detail=f"已检查 {len(required)} 个包" if not missing else f"缺失: {', '.join(missing)}",
        fix=f"pip install {' '.join(missing)}" if missing else "",
    )

    # ── 4. 网络连通 ───────────────────────────────────────────────
    net_ok = False
    try:
        import requests
        r = requests.get("https://paper-api.alpaca.markets/", timeout=5)
        net_ok = r.status_code < 500
        detail = f"HTTP {r.status_code} ({r.elapsed.total_seconds():.2f}s)"
    except Exception as e:
        detail = f"错误: {e}"

    check(
        "能连通 paper-api.alpaca.markets",
        net_ok,
        detail=detail,
        fix="检查网络 / 代理设置",
    )

    # ── 5. API 认证 ───────────────────────────────────────────────
    if key_ok and secret_ok and net_ok:
        auth_ok = False
        try:
            import requests
            r = requests.get(
                "https://paper-api.alpaca.markets/v2/account",
                headers={
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": secret_key,
                },
                timeout=10,
            )
            if r.status_code == 200:
                acct = r.json()
                auth_ok = True
                detail = (f"账户 {acct.get('account_number','?')} | "
                          f"equity=${float(acct.get('equity',0)):,.2f} | "
                          f"status={acct.get('status','?')}")
            else:
                detail = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            detail = f"错误: {e}"

        check(
            "Alpaca API 认证成功",
            auth_ok,
            detail=detail,
            fix="检查 key 是否 paper 账户、是否过期、是否手误（开头应该是 PK）",
        )
    else:
        print(f"{WARN} Alpaca API 认证  (跳过：前置检查未通过)")

    # ── 汇总 ──────────────────────────────────────────────────────
    print()
    print("━" * 64)
    if failures:
        print(f"  ❌ {len(failures)} 项失败：{', '.join(failures)}")
        print()
        print("  修复后重跑：python check_setup.py")
        print("  离线先看样子：python wheel_demo.py open")
        return 1
    else:
        print("  ✅ 全部通过 — 可以跑真数据了")
        print()
        print("  python wheel_summary.py open")
        print("  python wheel_health_check.py")
        return 0


if __name__ == "__main__":
    sys.exit(main())
