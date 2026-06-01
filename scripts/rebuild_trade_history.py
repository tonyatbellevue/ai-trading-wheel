"""Rebuild comprehensive wheel trade history from Alpaca order log.

Pairs each SELL (open short put) with its matching BUY (close), or marks as
'expired' if the put aged past expiration with no BTC.

Output: metrics/data/trade_history_full.csv
24+ fields per trade for future modeling/analysis:
    entry/exit dates+times, underlying, sector, contract, strike, DTE,
    duration, qty, prices, exit_type, collateral, premium in/out, net P&L,
    pnl_pct, annualized_return, is_win, order IDs.

Run any time to rebuild from current Alpaca history:
    python scripts/rebuild_trade_history.py

Used by future backtest comparison, ML feature engineering, and
post-mortem analyses.
"""
from __future__ import annotations
import sys, io, os, csv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from datetime import datetime, timezone, date as _date
from core.alpaca_client import AlpacaClients
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from strategy.wheel_strategy import _parse_symbol
from strategy.sector_map import sector_of

OUTPUT = "metrics/data/trade_history_full.csv"


def fetch_all_option_orders():
    """All closed option orders that actually filled."""
    trading = AlpacaClients.trading()
    print("Fetching closed orders from Alpaca...")
    req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, direction='asc')
    orders = trading.get_orders(filter=req)
    opt = []
    for o in orders:
        if not o.filled_at or not o.symbol or len(o.symbol) < 10:
            continue
        if float(o.filled_qty or 0) <= 0:
            continue
        info = _parse_symbol(o.symbol)
        if not info:
            continue
        opt.append({
            'order_id': o.id,
            'filled_at': o.filled_at,
            'symbol': o.symbol,
            'underlying': info['underlying'],
            'opt_type': info['type'],
            'strike': info['strike'],
            'expiry': info['expiry'],
            'side': str(o.side).replace("OrderSide.", ""),
            'qty': int(float(o.filled_qty)),
            'price': float(o.filled_avg_price),
            'limit_price': float(o.limit_price or 0),
            'sector': sector_of(info['underlying']),
        })
    return opt


def pair_trades(opt):
    """Match SELL → BUY (FIFO per symbol). Unmatched SELLs past expiry = expired."""
    open_pos = {}  # symbol → list of open entries
    trades = []

    for o in opt:
        if o['side'] == 'SELL':
            open_pos.setdefault(o['symbol'], []).append(o)
        elif o['side'] == 'BUY':
            queue = open_pos.get(o['symbol'])
            if not queue:
                print(f"⚠️  Orphan BUY: {o['symbol']} @ {o['filled_at']}")
                continue
            entry = queue.pop(0)
            trades.append(_make_trade(entry, o, 'BTC'))

    # Remaining open entries past expiry = expired
    today = _date.today()
    for sym, entries in open_pos.items():
        for entry in entries:
            if entry['expiry'] < today:
                trades.append(_make_trade(entry, None, 'expired'))

    trades.sort(key=lambda t: t['entry_time'])
    return trades


def _make_trade(entry, exit_order, exit_type):
    qty = entry['qty']
    if exit_order:
        qty = min(entry['qty'], exit_order['qty'])
        gross_out = exit_order['price'] * qty * 100
        exit_dt = exit_order['filled_at']
        exit_price = exit_order['price']
        exit_id = exit_order['order_id']
    else:
        gross_out = 0.0
        exit_dt = datetime.combine(entry['expiry'], datetime.min.time()).replace(tzinfo=timezone.utc).replace(hour=20)
        exit_price = 0.0
        exit_id = ''

    gross_in = entry['price'] * qty * 100
    pnl = gross_in - gross_out
    duration = max((exit_dt - entry['filled_at']).days, 0)
    dte_at_entry = (entry['expiry'] - entry['filled_at'].date()).days
    coll = entry['strike'] * 100 * qty

    return {
        'entry_date': entry['filled_at'].date().isoformat(),
        'entry_time': entry['filled_at'].isoformat(),
        'exit_date': exit_dt.date().isoformat(),
        'exit_time': exit_dt.isoformat(),
        'underlying': entry['underlying'],
        'sector': entry['sector'],
        'contract': entry['symbol'],
        'opt_type': 'Put' if entry['opt_type'] == 'P' else 'Call',
        'strike': entry['strike'],
        'expiry': entry['expiry'].isoformat(),
        'dte_at_entry': dte_at_entry,
        'duration_days': duration,
        'qty': qty,
        'entry_price': entry['price'],
        'exit_price': exit_price,
        'exit_type': exit_type,
        'collateral': coll,
        'premium_collected': gross_in,
        'premium_paid': gross_out,
        'net_pnl': pnl,
        'pnl_pct': (entry['price'] - exit_price) / entry['price'] if entry['price'] > 0 else 0,
        'annualized_return': (pnl / coll) * (365 / max(duration, 1)) if coll > 0 else 0,
        'is_win': 1 if pnl > 0 else 0,
        'entry_order_id': entry['order_id'],
        'exit_order_id': exit_id,
    }


def main():
    opt = fetch_all_option_orders()
    print(f"Filled option orders: {len(opt)}")

    trades = pair_trades(opt)
    if not trades:
        print("No closed trades to record.")
        return 0

    fieldnames = list(trades[0].keys())
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(trades)

    total_pnl = sum(t['net_pnl'] for t in trades)
    wins = sum(t['is_win'] for t in trades)
    print(f"\n✅ {len(trades)} 笔交易写入 {OUTPUT}")
    print(f"   胜率 {wins}/{len(trades)} ({wins/len(trades)*100:.0f}%) | 净 P&L ${total_pnl:+,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
