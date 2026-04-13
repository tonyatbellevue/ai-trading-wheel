"""账户仪表盘快捷入口"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from account.account_dashboard import AccountDashboard

if __name__ == "__main__":
    AccountDashboard().show_all()
