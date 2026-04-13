"""Wheel 仪表盘快捷入口：python show_wheel.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from account.wheel_dashboard import WheelDashboard

if __name__ == "__main__":
    WheelDashboard().show()
