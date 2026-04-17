"""AI Trading System — 统一交互式菜单入口

用法: python trading.py
"""
import sys
import os

# 确保项目根目录在 sys.path
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

MENU = """
========================================
       AI Trading System
========================================

  [账户 & 状态]
   1. 查看账户总览
   2. 查看 Wheel 仪表盘
   3. Wheel 健康检查
   4. 市场新闻摘要

  [实盘交易]
   5. 启动 AI 交易机器人 (EMA+RSI+ML)
   6. 执行一次 Wheel 策略
   7. 启动 Wheel 持续监控
   8. 启动 Wheel Web 仪表盘

  [回测 & 训练]
   9. 训练 ML 模型
  10. EMA+RSI 策略回测
  11. Wheel 策略回测 (单标的)
  12. Wheel 策略回测 (多标的)

  [通知]
  16. 发送开盘摘要邮件
  17. 发送收盘摘要邮件

  [工具]
  13. Wheel 标的切换管理
  14. Wheel 候选扫描
  15. Wheel 状态报告

   0. 退出
========================================"""


def _input(prompt, default=None):
    """带默认值的输入"""
    if default:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"{prompt}: ").strip()


def cmd_show_account():
    from account.account_dashboard import AccountDashboard
    AccountDashboard().show_all()


def cmd_show_wheel():
    from account.wheel_dashboard import WheelDashboard
    WheelDashboard().show()


def cmd_health_check():
    from wheel_health_check import run_health_check, format_markdown
    result = run_health_check()
    print(format_markdown(result))


def cmd_market_news():
    from market_news import build_market_news
    print(build_market_news())


def cmd_start_bot():
    print("启动 AI 交易机器人 (Ctrl+C 返回菜单)...")
    from main import TradingBot
    bot = TradingBot()
    bot.start()


def cmd_wheel_once():
    from wheel_once import main as wheel_main
    wheel_main()


def cmd_wheel_monitor():
    print("启动 Wheel 持续监控 (Ctrl+C 返回菜单)...")
    from run_wheel_csco import main as monitor_main
    monitor_main()


def cmd_wheel_web():
    print("启动 Wheel Web 仪表盘 (Ctrl+C 返回菜单)...")
    print("浏览器访问: http://127.0.0.1:5000")
    from wheel_server import app
    app.run(host="0.0.0.0", port=5000, debug=False)


def cmd_train_model():
    from train_model import main as train_main
    train_main()


def cmd_backtest_ema():
    use_ml = _input("使用 ML 过滤? (y/n)", "y").lower() == "y"
    symbols_str = _input("标的 (逗号分隔)", ",".join(__import__("config").settings.SYMBOLS))
    symbols = [s.strip().upper() for s in symbols_str.split(",")]
    start = _input("开始日期", __import__("config").settings.BACKTEST_START)
    end = _input("结束日期", __import__("config").settings.BACKTEST_END)

    from run_backtest import main as bt_main
    bt_main(use_ml=use_ml, symbols=symbols, start=start, end=end)


def cmd_wheel_backtest():
    symbol = _input("标的", "TSLA").upper()
    start = _input("开始日期", "2023-01-01")
    end = _input("结束日期", "2025-12-31")
    capital = float(_input("初始资金", "100000"))

    # 模拟 argparse
    sys.argv = ["run_wheel_backtest.py",
                "--symbol", symbol, "--start", start,
                "--end", end, "--capital", str(capital)]
    from run_wheel_backtest import main as wb_main
    wb_main()


def cmd_wheel_backtest_multi():
    from run_wheel_backtest_multi import main as wbm_main
    wbm_main()


def cmd_wheel_switch():
    print("Wheel 标的切换管理")
    print("  1. 查看状态")
    print("  2. 计划切换")
    print("  3. 取消计划")
    print("  4. 强制切换")
    choice = _input("选择", "1")

    from strategy.wheel_switch import load_state, save_state, plan_switch, cancel_plan

    if choice == "1":
        from wheel_switch_cli import show_status
        show_status()
    elif choice == "2":
        to_sym = _input("目标标的").upper()
        trigger = _input("触发日期 (YYYY-MM-DD)")
        reason = _input("原因 (可选)", "")
        s = load_state()
        plan_switch(s["active_symbol"], to_sym, trigger, reason)
        from wheel_switch_cli import show_status
        show_status()
    elif choice == "3":
        cancel_plan()
        from wheel_switch_cli import show_status
        show_status()
    elif choice == "4":
        to_sym = _input("目标标的").upper()
        confirm = _input(f"确认强制切换到 {to_sym}? (yes/no)", "no")
        if confirm.lower() == "yes":
            sys.argv = ["wheel_switch_cli.py", "force", to_sym]
            from wheel_switch_cli import main as switch_main
            switch_main()
        else:
            print("已取消")


def cmd_wheel_scan():
    from wheel_scanner import (
        scan_wheel_alternatives, format_wheel_alternatives_md,
        scan_csp_candidates, format_csp_md,
        scan_leaps_candidates, format_leaps_md,
    )
    print("扫描 Wheel 候选标的...")
    wheel_data = scan_wheel_alternatives(exclude="TSLA", top_n=2)
    print(format_wheel_alternatives_md(wheel_data))
    print()
    print("扫描 CSP 候选标的...")
    csp = scan_csp_candidates(top_n=3)
    print(format_csp_md(csp))
    print()
    print("扫描 LEAPS 候选标的...")
    leaps = scan_leaps_candidates(top_n=3)
    print(format_leaps_md(leaps))


def cmd_wheel_summary():
    print("报告类型:")
    print("  1. 开盘报告")
    print("  2. 收盘报告")
    print("  3. 交易报告")
    choice = _input("选择", "1")
    event_map = {"1": "open", "2": "close", "3": "trade"}
    event = event_map.get(choice, "open")

    from wheel_summary import build_summary
    print(build_summary(event))


def cmd_email_open():
    from email_summary import main as email_main
    email_main("open")


def cmd_email_close():
    from email_summary import main as email_main
    email_main("close")


COMMANDS = {
    "1": ("查看账户总览", cmd_show_account),
    "2": ("查看 Wheel 仪表盘", cmd_show_wheel),
    "3": ("Wheel 健康检查", cmd_health_check),
    "4": ("市场新闻摘要", cmd_market_news),
    "5": ("启动 AI 交易机器人", cmd_start_bot),
    "6": ("执行一次 Wheel 策略", cmd_wheel_once),
    "7": ("启动 Wheel 持续监控", cmd_wheel_monitor),
    "8": ("启动 Wheel Web 仪表盘", cmd_wheel_web),
    "9": ("训练 ML 模型", cmd_train_model),
    "10": ("EMA+RSI 策略回测", cmd_backtest_ema),
    "11": ("Wheel 策略回测 (单标的)", cmd_wheel_backtest),
    "12": ("Wheel 策略回测 (多标的)", cmd_wheel_backtest_multi),
    "13": ("Wheel 标的切换管理", cmd_wheel_switch),
    "14": ("Wheel 候选扫描", cmd_wheel_scan),
    "15": ("Wheel 状态报告", cmd_wheel_summary),
    "16": ("发送开盘摘要邮件", cmd_email_open),
    "17": ("发送收盘摘要邮件", cmd_email_close),
}


def main():
    while True:
        print(MENU)
        choice = input("\n请输入编号: ").strip()

        if choice == "0":
            print("再见!")
            break

        if choice not in COMMANDS:
            print(f"无效选项: {choice}")
            continue

        name, func = COMMANDS[choice]
        print(f"\n--- {name} ---\n")
        try:
            func()
        except KeyboardInterrupt:
            print("\n\n已中断，返回菜单...")
        except Exception as e:
            print(f"\n执行出错: {e}")

        print()
        input("按回车返回菜单...")


if __name__ == "__main__":
    main()
