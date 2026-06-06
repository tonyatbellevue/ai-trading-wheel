"""系统健康检查 — 每日自动验证关键依赖

检查项：
  1. Alpaca API key 有效
  2. 期权数据流可用
  3. 邮件 SMTP 凭证可用
  4. .env 配置完整
  5. 磁盘空间足够（reports/ + metrics/data/）
  6. 待发送的失败邮件队列长度

输出：
  - stdout: 人类可读的报告
  - 退出码 0=全部通过, 1=有警告, 2=有错误
  - 如果有错误，自动发邮件告警

用法：
    python scripts/health_check.py
    python scripts/health_check.py --silent     # 只在有问题时输出
    python scripts/health_check.py --no-email   # 不发告警邮件
"""
from __future__ import annotations
import sys
import os
import argparse
import shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import settings
from loguru import logger


class HealthCheck:
    def __init__(self):
        self.checks: list[dict] = []
        self.errors = 0
        self.warnings = 0

    def add(self, name: str, status: str, message: str = ""):
        """status: PASS | WARN | FAIL"""
        self.checks.append({"name": name, "status": status, "message": message})
        if status == "FAIL":
            self.errors += 1
        elif status == "WARN":
            self.warnings += 1

    # ── 各项检查 ────────────────────────────────────────────────────

    def check_env(self):
        """配置完整性"""
        required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY",
                    "EMAIL_SMTP_SERVER", "EMAIL_SENDER",
                    "EMAIL_PASSWORD", "EMAIL_RECIPIENT"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            self.add("env_config", "FAIL", f"缺少环境变量: {', '.join(missing)}")
        else:
            env_path = getattr(settings, "_ENV_LOADED_FROM", "?")
            self.add("env_config", "PASS", f"加载自 {env_path}")

    def check_alpaca_api(self):
        """Alpaca trading API 可用"""
        try:
            from core.alpaca_client import AlpacaClients
            acct = AlpacaClients.trading().get_account()
            equity = float(acct.equity)
            cash = float(acct.cash)
            self.add("alpaca_trading", "PASS",
                     f"权益 ${equity:,.2f} | 现金 ${cash:,.2f}")
        except Exception as e:
            self.add("alpaca_trading", "FAIL", f"401 unauthorized 或网络错误: {e}")

    def check_alpaca_data(self):
        """股票/期权数据流"""
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            stk = StockHistoricalDataClient(api_key=settings.API_KEY,
                                             secret_key=settings.SECRET_KEY)
            quote = stk.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols="SPY"))
            spy_price = float(quote["SPY"].ask_price or quote["SPY"].bid_price)
            self.add("alpaca_data", "PASS", f"SPY=${spy_price:.2f}")
        except Exception as e:
            self.add("alpaca_data", "FAIL", f"数据 API 错误: {e}")

    def check_smtp(self):
        """SMTP 邮件凭证（不实际发送）"""
        try:
            import smtplib
            import ssl
            with smtplib.SMTP(settings.EMAIL_SMTP_SERVER,
                              settings.EMAIL_SMTP_PORT, timeout=10) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(settings.EMAIL_SENDER, settings.EMAIL_PASSWORD)
            self.add("smtp_auth", "PASS",
                     f"{settings.EMAIL_SMTP_SERVER}:{settings.EMAIL_SMTP_PORT}")
        except Exception as e:
            self.add("smtp_auth", "FAIL", f"SMTP 登录失败: {e}")

    def check_disk(self):
        """磁盘空间"""
        try:
            base = Path(settings.BASE_DIR)
            stat = shutil.disk_usage(base)
            free_gb = stat.free / (1024**3)
            if free_gb < 1:
                self.add("disk_space", "FAIL", f"剩余 {free_gb:.2f}GB < 1GB")
            elif free_gb < 5:
                self.add("disk_space", "WARN", f"剩余 {free_gb:.2f}GB")
            else:
                self.add("disk_space", "PASS", f"剩余 {free_gb:.1f}GB")
        except Exception as e:
            self.add("disk_space", "WARN", str(e))

    def check_failed_email_queue(self):
        """待发送邮件队列"""
        try:
            from utils.emailer import FAILED_DIR
            files = list(FAILED_DIR.glob("*.json"))
            count = len(files)
            if count == 0:
                self.add("email_queue", "PASS", "队列为空")
            elif count < 5:
                self.add("email_queue", "WARN", f"{count} 封待重试")
            else:
                self.add("email_queue", "FAIL",
                         f"{count} 封待重试，请检查 SMTP 配置")
        except Exception as e:
            self.add("email_queue", "WARN", str(e))

    def check_wheel_state(self):
        """Wheel 标的状态文件存在"""
        try:
            wheel_json = Path(settings.BASE_DIR) / "wheel_symbol.json"
            if wheel_json.exists():
                import json
                state = json.loads(wheel_json.read_text(encoding="utf-8"))
                self.add("wheel_state", "PASS",
                         f"活跃: {state.get('active_symbol', '?')}")
            else:
                self.add("wheel_state", "WARN", "wheel_symbol.json 不存在")
        except Exception as e:
            self.add("wheel_state", "WARN", str(e))

    # ── 报告 ────────────────────────────────────────────────────────

    def run_shadow_rotation(self):
        """每日运行 shadow rotation 快照（虚拟 v3 逻辑）"""
        try:
            from metrics.shadow_rotation import daily_snapshot
            result = daily_snapshot()
            if result.get("status") == "ok":
                self.add("shadow_rotation", "PASS",
                         f"虚拟权益 ${result['shadow_equity']:,.2f} | "
                         f"{result['shadow_symbol']}/{result['shadow_phase']} | "
                         f"累计轮换 {result['rotations_total']}")
            else:
                self.add("shadow_rotation", "WARN", f"状态: {result.get('status')}")
        except Exception as e:
            self.add("shadow_rotation", "WARN", f"shadow 跳过: {e}")

    def run_assignment_postmortem(self):
        """扫最近 14 天的 assignment/expiration，为每个新事件生成复盘报告"""
        try:
            from metrics.assignment_postmortem import detect_and_analyze
            new_reports = detect_and_analyze(since_days=14)
            if new_reports:
                names = ", ".join(p.name for p in new_reports[:3])
                self.add("postmortem", "PASS",
                         f"{len(new_reports)} 份新复盘已生成: {names}")
            else:
                self.add("postmortem", "PASS", "无新 assignment/expiration 事件")
        except Exception as e:
            self.add("postmortem", "WARN", f"复盘生成失败: {e}")

    def run_multi_wheel_simulator(self):
        """跑 scripts/simulate_multi_wheel.py 的 16 个回归场景。
        任何 FAIL 说明 wheel_once / wheel_strategy 改动破坏了多 wheel 管理。
        见 PR #99 (MSFT 孤儿 bug) 和 PR #100 (回归测试 harness)。
        """
        try:
            import subprocess
            script = Path(__file__).parent / "simulate_multi_wheel.py"
            r = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
            stdout = r.stdout or ""
            if r.returncode == 0:
                passes = stdout.count("[PASS]")
                self.add("multi_wheel_sim", "PASS", f"{passes} 场景全过")
            else:
                fails = [l for l in stdout.splitlines() if "[FAIL]" in l or "    -" in l]
                detail = " | ".join(fails[:5]) or "(see logs)"
                self.add("multi_wheel_sim", "FAIL",
                         f"模拟器有场景失败 → 多 wheel 逻辑可能回归. {detail}")
        except Exception as e:
            self.add("multi_wheel_sim", "WARN", f"模拟器跑失败: {e}")

    def run_stock_stop_loss_test(self):
        """跑 Bug #2 回归测试: -35% 股票止损的数据校验。"""
        try:
            import subprocess
            script = Path(__file__).parent / "test_stock_stop_loss.py"
            r = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
            stdout = r.stdout or ""
            if r.returncode == 0:
                self.add("stock_stop_loss", "PASS",
                         f"{stdout.count('[PASS]')} 数据校验用例全过")
            else:
                fails = [l for l in stdout.splitlines() if "[FAIL]" in l]
                self.add("stock_stop_loss", "FAIL",
                         f"-35% 止损数据校验回归! {' | '.join(fails[:3]) or '(see logs)'}")
        except Exception as e:
            self.add("stock_stop_loss", "WARN", f"止损测试跑失败: {e}")

    def run_intrinsic_bound_test(self):
        """Phase 3 内在价值边界回归测试。"""
        try:
            import subprocess
            script = Path(__file__).parent / "test_intrinsic_bound.py"
            r = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
            stdout = r.stdout or ""
            if r.returncode == 0:
                self.add("intrinsic_bound", "PASS",
                         f"{stdout.count('[PASS]')} 边界用例全过")
            else:
                fails = [l for l in stdout.splitlines() if "[FAIL]" in l]
                self.add("intrinsic_bound", "FAIL",
                         f"内在价值边界回归! {' | '.join(fails[:3]) or '(see logs)'}")
        except Exception as e:
            self.add("intrinsic_bound", "WARN", f"边界测试跑失败: {e}")

    def run_escalating_stop_test(self):
        """Phase 4.1 升级止损回归测试。"""
        try:
            import subprocess
            script = Path(__file__).parent / "test_escalating_stop.py"
            r = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
            stdout = r.stdout or ""
            if r.returncode == 0:
                self.add("escalating_stop", "PASS",
                         f"{stdout.count('[PASS]')} 升级止损用例全过")
            else:
                fails = [l for l in stdout.splitlines() if "[FAIL]" in l]
                self.add("escalating_stop", "FAIL",
                         f"升级止损回归! {' | '.join(fails[:3]) or '(see logs)'}")
        except Exception as e:
            self.add("escalating_stop", "WARN", f"升级止损测试跑失败: {e}")

    def run_crash_timeline_sim(self):
        """Phase 4.1 端到端崩盘时间线模拟 (跨 tick 状态机)。"""
        try:
            import subprocess
            script = Path(__file__).parent / "sim_crash_timeline.py"
            r = subprocess.run([sys.executable, str(script)],
                                capture_output=True, text=True, timeout=60,
                                encoding="utf-8", errors="replace")
            if r.returncode == 0:
                self.add("crash_timeline_sim", "PASS", "4 个崩盘时间线场景全过")
            else:
                self.add("crash_timeline_sim", "FAIL",
                         "崩盘时间线模拟失败 → 升级止损状态机回归")
        except Exception as e:
            self.add("crash_timeline_sim", "WARN", f"崩盘模拟跑失败: {e}")

    def report_quote_pass_rate(self):
        """放行率监控: fair_price 放行率太低 = 过度防护妨碍执行。"""
        try:
            from metrics.quote_stats import summary
            s = summary()
            total = s["total"]
            if total == 0:
                self.add("quote_pass_rate", "PASS", "今日暂无 fair_price 调用")
                return
            rate = s["pass_rate"]
            bd = s["breakdown"]
            detail = (f"放行 {rate:.0%} ({s['used']}/{total}) | "
                      f"last={bd.get('last',0)} mid={bd.get('mid',0)} "
                      f"拒绝-边界={bd.get('reject_bound',0)} 拒绝-无报价={bd.get('reject_none',0)}")
            # 低放行率 = 过度防护警告
            if rate < 0.70 and total >= 10:
                self.add("quote_pass_rate", "WARN",
                         f"放行率偏低 → 可能过度防护妨碍 SL/TP 执行. {detail}")
            else:
                self.add("quote_pass_rate", "PASS", detail)
        except Exception as e:
            self.add("quote_pass_rate", "WARN", f"放行率统计失败: {e}")

    def run_all(self):
        self.check_env()
        self.check_alpaca_api()
        self.check_alpaca_data()
        self.check_smtp()
        self.check_disk()
        self.check_failed_email_queue()
        self.check_wheel_state()
        self.run_multi_wheel_simulator()
        self.run_stock_stop_loss_test()
        self.run_intrinsic_bound_test()
        self.run_escalating_stop_test()
        self.run_crash_timeline_sim()
        self.report_quote_pass_rate()
        self.run_shadow_rotation()
        self.run_assignment_postmortem()

    def format_report(self) -> str:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"# AI Trading 系统健康检查",
            f"**{ts}**",
            "",
            "| 检查项 | 状态 | 详情 |",
            "|--------|------|------|",
        ]
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}
        for c in self.checks:
            lines.append(
                f"| {c['name']} | {icon.get(c['status'], '?')} {c['status']} | {c['message']} |"
            )
        lines += [
            "",
            f"**总结：** {len(self.checks) - self.errors - self.warnings} 通过 / "
            f"{self.warnings} 警告 / {self.errors} 错误",
        ]
        return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--silent", action="store_true", help="只在有问题时输出")
    p.add_argument("--no-email", action="store_true", help="不发告警邮件")
    args = p.parse_args()

    hc = HealthCheck()
    hc.run_all()
    report = hc.format_report()

    if not args.silent or hc.errors > 0:
        print(report)

    # 有错误且未禁用邮件 → 发告警
    if hc.errors > 0 and not args.no_email:
        try:
            from utils.emailer import send_email, md_to_html
            send_email(
                subject=f"⚠️ AI Trading 健康检查失败 ({hc.errors} 错)",
                body_text=report,
                body_html=md_to_html(report),
            )
        except Exception as e:
            print(f"告警邮件发送失败: {e}", file=sys.stderr)

    return 2 if hc.errors > 0 else (1 if hc.warnings > 0 else 0)


if __name__ == "__main__":
    sys.exit(main())
