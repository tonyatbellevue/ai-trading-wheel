"""Wheel 策略 Web 仪表盘 — python wheel_server.py"""
import json
from datetime import date
from flask import Flask, jsonify

from config import settings
from core.alpaca_client import AlpacaClients
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockLatestQuoteRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from execution.option_order_manager import OptionOrderManager
from strategy.wheel_strategy import WheelStrategy, WheelPhase, _parse_symbol

app = Flask(__name__)
SYMBOL = settings.WHEEL_SYMBOL


def _get_dashboard_data() -> dict:
    trading  = AlpacaClients.trading()
    opt_data = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    stk_data = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    opt_mgr  = OptionOrderManager()
    strategy = WheelStrategy(SYMBOL)

    # ── Phase ───────────────────────────────────────────────────
    phase, phase_obj = strategy.get_phase()

    # ── Stock price ─────────────────────────────────────────────
    stock_price = None
    try:
        resp = stk_data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=SYMBOL))
        q = resp[SYMBOL]
        stock_price = round((float(q.bid_price) + float(q.ask_price)) / 2, 2)
    except Exception:
        pass

    # ── Account ─────────────────────────────────────────────────
    acct = trading.get_account()
    account = {
        "equity": float(acct.equity),
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
    }

    # ── Stock position ──────────────────────────────────────────
    stock_pos = None
    try:
        pos = trading.get_open_position(SYMBOL)
        stock_pos = {
            "qty": float(pos.qty),
            "avg_entry": float(pos.avg_entry_price),
            "market_value": float(pos.market_value),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": float(pos.unrealized_plpc) * 100,
        }
    except Exception:
        pass

    # ── Option positions ────────────────────────────────────────
    option_positions = []
    raw_positions = opt_mgr.get_option_positions(SYMBOL)
    if raw_positions:
        syms = [p.symbol for p in raw_positions]
        snaps = {}
        try:
            snaps = opt_data.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=syms))
        except Exception:
            pass
        today = date.today()
        for pos in raw_positions:
            info = _parse_symbol(pos.symbol)
            if not info:
                continue
            snap = snaps.get(pos.symbol)
            qty  = float(pos.qty)
            cost = float(pos.avg_entry_price or 0)
            dte  = (info["expiry"] - today).days

            curr, delta = 0.0, None
            if snap and snap.latest_quote:
                bid = float(snap.latest_quote.bid_price or 0)
                ask = float(snap.latest_quote.ask_price or 0)
                curr = round((bid + ask) / 2, 2)
            if snap and snap.greeks and snap.greeks.delta is not None:
                delta = round(snap.greeks.delta, 4)

            pl = round((cost - curr) * abs(qty) * 100, 2) if qty < 0 \
                 else round((curr - cost) * abs(qty) * 100, 2)

            otm_pad = None
            if stock_price:
                if info["type"] == "P":
                    otm_pad = round(stock_price - info["strike"], 2)
                else:
                    otm_pad = round(info["strike"] - stock_price, 2)

            option_positions.append({
                "symbol": pos.symbol,
                "type": "PUT" if info["type"] == "P" else "CALL",
                "strike": info["strike"],
                "expiry": str(info["expiry"]),
                "dte": dte,
                "qty": qty,
                "cost": cost,
                "current": curr,
                "pl": pl,
                "delta": delta,
                "otm_pad": otm_pad,
            })

    # ── Open orders ─────────────────────────────────────────────
    raw_open_orders = opt_mgr.get_open_option_orders(SYMBOL)
    open_order_syms = [o.symbol for o in raw_open_orders]
    open_order_snaps = {}
    if open_order_syms:
        try:
            open_order_snaps = opt_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=open_order_syms)
            )
        except Exception:
            pass

    open_orders = []
    for o in raw_open_orders:
        info = _parse_symbol(o.symbol)
        dte  = (info["expiry"] - date.today()).days if info else 0
        snap = open_order_snaps.get(o.symbol)
        bid_price, ask_price, mid_price = None, None, None
        if snap and snap.latest_quote:
            bid_price = float(snap.latest_quote.bid_price or 0)
            ask_price = float(snap.latest_quote.ask_price or 0)
            mid_price = round((bid_price + ask_price) / 2, 2)
        open_orders.append({
            "symbol": o.symbol,
            "side": str(o.side).split(".")[-1],
            "qty": float(o.qty or 1),
            "limit_price": float(o.limit_price) if o.limit_price else None,
            "bid": bid_price,
            "ask": ask_price,
            "mid": mid_price,
            "status": str(o.status).split(".")[-1],
            "dte": dte,
            "type": ("PUT" if info["type"] == "P" else "CALL") if info else "?",
            "strike": info["strike"] if info else 0,
            "expiry": str(info["expiry"]) if info else "",
        })

    # ── History ─────────────────────────────────────────────────
    history = []
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=50)
        all_orders = trading.get_orders(req)
        for o in all_orders:
            if o.symbol.startswith(SYMBOL) and len(o.symbol) > 6:
                info = _parse_symbol(o.symbol)
                history.append({
                    "time": str(o.submitted_at)[:19] if o.submitted_at else "",
                    "symbol": o.symbol,
                    "side": str(o.side).split(".")[-1],
                    "filled_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                    "status": str(o.status).split(".")[-1],
                    "type": ("PUT" if info["type"] == "P" else "CALL") if info else "?",
                    "strike": info["strike"] if info else 0,
                })
    except Exception:
        pass

    # ── Summary ─────────────────────────────────────────────────
    total_premium, total_buyback, rounds = 0.0, 0.0, 0
    for h in history:
        if h["filled_price"]:
            val = h["filled_price"] * 100
            if h["side"] == "SELL":
                total_premium += val
                rounds += 1
            else:
                total_buyback += val

    # ── Market clock ──────────────────────────────────────────────
    try:
        clock = trading.get_clock()
        market_clock = {
            "is_open": clock.is_open,
            "timestamp": str(clock.timestamp)[:19],
            "next_close": str(clock.next_close)[:19],
        }
    except Exception:
        market_clock = {"is_open": False, "timestamp": "", "next_close": ""}

    return {
        "symbol": SYMBOL,
        "stock_price": stock_price,
        "phase": phase.name,
        "market_clock": market_clock,
        "account": account,
        "stock_position": stock_pos,
        "option_positions": option_positions,
        "open_orders": open_orders,
        "history": history,
        "summary": {
            "total_premium": total_premium,
            "total_buyback": total_buyback,
            "realized_pl": total_premium - total_buyback,
            "rounds": rounds,
        },
        "settings": {
            "target_delta": settings.WHEEL_TARGET_DELTA,
            "min_dte": settings.WHEEL_MIN_DTE,
            "max_dte": settings.WHEEL_MAX_DTE,
            "contracts": settings.WHEEL_CONTRACTS,
        },
    }


@app.route("/api/data")
def api_data():
    try:
        return jsonify(_get_dashboard_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


import time as _time
_cache = {"data": None, "ts": 0}

@app.route("/")
def index():
    now = _time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > 20:
        _cache["data"] = _get_dashboard_data()
        _cache["ts"] = now
    return _render_html(_cache["data"])


def _render_html(d: dict) -> str:
    mc   = d.get("market_clock", {})
    is_open   = mc.get("is_open", False)
    et_time   = mc.get("timestamp", "")[:19]
    next_close = mc.get("next_close", "")
    next_open  = mc.get("next_open", "")
    refresh_sec = 0  # 禁用 auto-refresh，手动刷新即可
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh_sec}">' if refresh_sec else ""

    s       = d.get("summary", {})
    acct    = d.get("account", {})
    sp      = d.get("stock_position")
    phase   = d.get("phase", "IDLE")
    price   = d.get("stock_price")
    sym     = d.get("symbol", "")
    cfg     = d.get("settings", {})

    phase_labels = {
        "IDLE":       ("[ ] IDLE", "phase-idle"),
        "SHORT_PUT":  ("[P] SHORT PUT", "phase-put"),
        "LONG_STOCK": ("[S] LONG STOCK", "phase-stock"),
        "SHORT_CALL": ("[C] SHORT CALL", "phase-call"),
    }

    def phase_bar():
        steps = ["IDLE", "SHORT_PUT", "LONG_STOCK", "SHORT_CALL"]
        labels = {"IDLE":"IDLE<br><small>wait</small>",
                  "SHORT_PUT":"SHORT PUT<br><small>sell CSP</small>",
                  "LONG_STOCK":"LONG STOCK<br><small>assigned</small>",
                  "SHORT_CALL":"SHORT CALL<br><small>sell CC</small>"}
        cls_map = {"IDLE":"idle","SHORT_PUT":"short-put","LONG_STOCK":"long-stock","SHORT_CALL":"short-call"}
        html = ""
        for st in steps:
            active = "active " + cls_map[st] if st == phase else ""
            html += f'<div class="phase-step {active}">{labels[st]}</div>'
        return html

    def fmt(v):
        if v is None: return "—"
        return f"${v:,.2f}"

    def pct(v):
        if v is None: return ""
        return f"({'+'if v>=0 else ''}{v:.2f}%)"

    def pl_cls(v):
        if v is None: return ""
        return "positive" if v >= 0 else "negative"

    # cards
    cards_html = f"""
    <div class="card"><div class="label">STOCK PRICE</div><div class="value">{fmt(price)}</div></div>
    <div class="card"><div class="label">CASH</div><div class="value">{fmt(acct.get('cash'))}</div></div>
    <div class="card"><div class="label">EQUITY</div><div class="value">{fmt(acct.get('equity'))}</div></div>
    <div class="card"><div class="label">PREMIUM COLLECTED</div><div class="value green">{fmt(s.get('total_premium'))}</div></div>
    <div class="card"><div class="label">REALIZED P&amp;L</div><div class="value {pl_cls(s.get('realized_pl'))}">{fmt(s.get('realized_pl'))}</div></div>
    <div class="card"><div class="label">ROUNDS</div><div class="value blue">{s.get('rounds',0)}</div></div>
    """
    if sp:
        cards_html += f'<div class="card"><div class="label">{sym} STOCK ({sp["qty"]:.0f} sh)</div><div class="value {pl_cls(sp["unrealized_pl"])}">{fmt(sp["unrealized_pl"])} {pct(sp["unrealized_plpc"])}</div></div>'

    # option positions
    opt_rows = ""
    for p in d.get("option_positions", []):
        otm_v = p.get("otm_pad")
        if otm_v is not None:
            otm_cls = "otm-safe" if otm_v >= 0 else "otm-danger"
            otm_lbl = "OTM" if otm_v >= 0 else "ITM"
            otm_str = f'<span class="otm-badge {otm_cls}">{otm_lbl} ${abs(otm_v):.2f}</span>'
        else:
            otm_str = "—"
        pl_v = p.get("pl", 0)
        delta_v = p.get("delta")
        opt_rows += f"""<tr>
            <td class="mono">{p['symbol']}</td>
            <td><span class="tag tag-{p['type'].lower()}">{p['type']}</span></td>
            <td>{fmt(p['strike'])}</td><td>{p['expiry']}</td><td>{p['dte']}</td>
            <td>{p['qty']:.0f}</td><td>{fmt(p['cost'])}</td><td>{fmt(p.get('current'))}</td>
            <td class="{pl_cls(pl_v)}">{fmt(pl_v)}</td>
            <td>{f"{delta_v:.4f}" if delta_v is not None else "—"}</td>
            <td>{otm_str}</td></tr>"""
    opt_section = f"""<table><tr>
        <th>CONTRACT</th><th>TYPE</th><th>STRIKE</th><th>EXPIRY</th><th>DTE</th>
        <th>QTY</th><th>COST</th><th>CURRENT</th><th>P&amp;L</th><th>DELTA</th><th>OTM</th></tr>
        {opt_rows}</table>""" if opt_rows else '<div class="empty">No option positions</div>'

    # open orders
    ord_rows = ""
    for o in d.get("open_orders", []):
        stat_cls = o['status'].lower()
        ord_rows += f"""<tr>
            <td class="mono">{o['symbol']}</td>
            <td><span class="tag tag-{o['type'].lower()}">{o['type']}</span></td>
            <td><span class="tag tag-{o['side'].lower()}">{o['side']}</span></td>
            <td>{fmt(o['strike'])}</td><td>{o['expiry']}</td><td>{o['dte']}</td><td>{o['qty']:.0f}</td>
            <td>{fmt(o.get('limit_price'))}</td>
            <td style="color:var(--red)">{fmt(o.get('bid'))}</td>
            <td style="color:var(--green)">{fmt(o.get('ask'))}</td>
            <td>{fmt(o.get('mid'))}</td>
            <td><span class="status status-{stat_cls}">{o['status']}</span></td></tr>"""
    ord_section = f"""<table><tr>
        <th>CONTRACT</th><th>TYPE</th><th>SIDE</th><th>STRIKE</th><th>EXPIRY</th><th>DTE</th>
        <th>QTY</th><th>LIMIT</th><th>BID</th><th>ASK</th><th>MID</th><th>STATUS</th></tr>
        {ord_rows}</table>""" if ord_rows else '<div class="empty">No open orders</div>'

    # history
    hist_rows = ""
    for h in d.get("history", []):
        stat_cls = h['status'].lower()
        price_str = fmt(h['filled_price']) if h['filled_price'] else '<span style="color:var(--dim)">pending</span>'
        hist_rows += f"""<tr>
            <td class="mono" style="font-size:11px">{h['time']}</td>
            <td class="mono">{h['symbol']}</td>
            <td><span class="tag tag-{h['type'].lower()}">{h['type']}</span></td>
            <td><span class="tag tag-{h['side'].lower()}">{h['side']}</span></td>
            <td>{price_str}</td>
            <td><span class="status status-{stat_cls}">{h['status']}</span></td></tr>"""
    hist_section = f"""<table><tr>
        <th>TIME</th><th>CONTRACT</th><th>TYPE</th><th>SIDE</th><th>PRICE</th><th>STATUS</th></tr>
        {hist_rows}</table>""" if hist_rows else '<div class="empty">No trade history</div>'

    # summary
    summary_html = f"""
    <div class="summary-item"><span class="sl">Premium Collected (STO)</span><span class="sv positive">{fmt(s.get('total_premium'))}</span></div>
    <div class="summary-item"><span class="sl">Buyback Cost (BTC)</span><span class="sv negative">{fmt(s.get('total_buyback'))}</span></div>
    <div class="summary-item"><span class="sl">Realized P&amp;L</span><span class="sv {pl_cls(s.get('realized_pl'))}">{fmt(s.get('realized_pl'))}</span></div>
    <div class="summary-item"><span class="sl">Rounds Completed</span><span class="sv">{s.get('rounds',0)}</span></div>
    """

    market_dot = "market-open" if is_open else "market-closed"
    market_txt = "MARKET OPEN" if is_open else "MARKET CLOSED"
    time_info = f"Closes: {next_close[11:16]}" if is_open and next_close else (f"Next open: {next_open[:16]}" if next_open else "")
    refresh_info = f"Auto-refresh in {refresh_sec}s" if is_open else "No refresh outside market hours"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">{meta_refresh}
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Option Wheel Dashboard</title>
<style>
:root{{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:20px;}}
.header{{text-align:center;margin-bottom:20px;}}
.header h1{{font-size:22px;font-weight:600;color:var(--accent);margin-bottom:6px;}}
.header .sub{{color:var(--dim);font-size:13px;}}
.market-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle;}}
.market-open{{background:var(--green);box-shadow:0 0 6px var(--green);}}
.market-closed{{background:var(--red);}}
.phase-bar{{display:flex;justify-content:center;gap:0;margin:16px auto 20px;max-width:700px;}}
.phase-step{{flex:1;text-align:center;padding:10px 8px;font-size:12px;font-weight:600;background:var(--surface);border:1px solid var(--border);color:var(--dim);}}
.phase-step:first-child{{border-radius:8px 0 0 8px;}}
.phase-step:last-child{{border-radius:0 8px 8px 0;}}
.phase-step.active.idle{{background:var(--dim);border-color:var(--dim);color:#fff;}}
.phase-step.active.short-put{{background:var(--blue);border-color:var(--blue);color:#fff;}}
.phase-step.active.long-stock{{background:var(--yellow);border-color:var(--yellow);color:#fff;}}
.phase-step.active.short-call{{background:var(--green);border-color:var(--green);color:#fff;}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px;}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center;}}
.card .label{{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;}}
.card .value{{font-size:22px;font-weight:700;}}
.green{{color:var(--green);}} .red{{color:var(--red);}} .blue{{color:var(--accent);}}
.positive{{color:var(--green);}} .negative{{color:var(--red);}}
.section{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;}}
.section h3{{font-size:14px;font-weight:600;color:var(--accent);margin-bottom:12px;}}
.empty{{color:var(--dim);font-size:13px;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
th{{text-align:left;color:var(--dim);font-weight:500;padding:7px 10px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase;letter-spacing:.5px;}}
td{{padding:7px 10px;border-bottom:1px solid var(--border);}}
tr:last-child td{{border-bottom:none;}}
.tag{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600;}}
.tag-put{{background:rgba(88,166,255,.15);color:var(--blue);}}
.tag-call{{background:rgba(63,185,80,.15);color:var(--green);}}
.tag-sell{{background:rgba(248,81,73,.12);color:var(--red);}}
.tag-buy{{background:rgba(63,185,80,.12);color:var(--green);}}
.status{{font-size:11px;padding:2px 6px;border-radius:3px;}}
.status-filled{{background:rgba(63,185,80,.15);color:var(--green);}}
.status-new{{background:rgba(210,153,34,.15);color:var(--yellow);}}
.status-canceled{{background:rgba(139,148,158,.15);color:var(--dim);}}
.mono{{font-family:'Cascadia Code','Consolas',monospace;font-size:11px;}}
.otm-badge{{font-size:11px;padding:1px 6px;border-radius:3px;}}
.otm-safe{{background:rgba(63,185,80,.12);color:var(--green);}}
.otm-danger{{background:rgba(248,81,73,.12);color:var(--red);}}
.summary-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;}}
.summary-item{{display:flex;justify-content:space-between;padding:6px 0;}}
.summary-item .sl{{color:var(--dim);font-size:13px;}}
.summary-item .sv{{font-weight:600;font-size:14px;}}
.footer{{text-align:center;color:var(--dim);font-size:11px;margin-top:16px;}}
</style>
</head>
<body>
<div class="header">
  <h1>Option Wheel Dashboard</h1>
  <div class="sub"><span class="market-dot {market_dot}"></span>{market_txt} &nbsp;|&nbsp; {sym} {fmt(price)} &nbsp;|&nbsp; {et_time} ET &nbsp;|&nbsp; {time_info} &nbsp;|&nbsp; {refresh_info}</div>
</div>
<div class="phase-bar">{phase_bar()}</div>
<div class="cards">{cards_html}</div>
<div class="section"><h3>Option Positions</h3>{opt_section}</div>
<div class="section"><h3>Open Orders</h3>{ord_section}</div>
<div class="section"><h3>Revenue Summary</h3><div class="summary-grid">{summary_html}</div></div>
<div class="section"><h3>Trade History</h3>{hist_section}</div>
<div class="footer">Target Delta: &#177;{cfg.get('target_delta','—')} &nbsp;|&nbsp; DTE: {cfg.get('min_dte','—')}~{cfg.get('max_dte','—')} &nbsp;|&nbsp; Contracts: {cfg.get('contracts','—')}</div>
</body></html>"""


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Option Wheel Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }

  .header { text-align: center; margin-bottom: 24px; }
  .header h1 { font-size: 22px; font-weight: 600; color: var(--accent); margin-bottom: 6px; }
  .header .sub { color: var(--dim); font-size: 13px; }
  .market-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }
  .market-open { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .market-closed { background: var(--red); }

  .phase-bar {
    display: flex; justify-content: center; gap: 0; margin: 20px auto; max-width: 700px;
  }
  .phase-step {
    flex: 1; text-align: center; padding: 10px 8px; font-size: 12px; font-weight: 600;
    background: var(--surface); border: 1px solid var(--border); color: var(--dim);
    position: relative;
  }
  .phase-step:first-child { border-radius: 8px 0 0 8px; }
  .phase-step:last-child { border-radius: 0 8px 8px 0; }
  .phase-step.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .phase-step.active.idle { background: var(--dim); border-color: var(--dim); }
  .phase-step.active.short-put { background: var(--blue); border-color: var(--blue); }
  .phase-step.active.long-stock { background: var(--yellow); border-color: var(--yellow); }
  .phase-step.active.short-call { background: var(--green); border-color: var(--green); }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 24px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; text-align: center;
  }
  .card .label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
  .card .value { font-size: 24px; font-weight: 700; }
  .card .value.green { color: var(--green); }
  .card .value.red { color: var(--red); }
  .card .value.blue { color: var(--accent); }

  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px; margin-bottom: 18px; }
  .section h3 { font-size: 14px; font-weight: 600; color: var(--accent); margin-bottom: 12px; }
  .section .empty { color: var(--dim); font-size: 13px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--dim); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  .tag {
    display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
  }
  .tag-put { background: rgba(88,166,255,0.15); color: var(--blue); }
  .tag-call { background: rgba(63,185,80,0.15); color: var(--green); }
  .tag-sell { background: rgba(248,81,73,0.12); color: var(--red); }
  .tag-buy { background: rgba(63,185,80,0.12); color: var(--green); }
  .status { font-size: 11px; padding: 2px 6px; border-radius: 3px; }
  .status-filled { background: rgba(63,185,80,0.15); color: var(--green); }
  .status-new { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .status-canceled { background: rgba(139,148,158,0.15); color: var(--dim); }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .mono { font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 12px; }

  .otm-badge { font-size: 11px; margin-left: 6px; padding: 1px 6px; border-radius: 3px; }
  .otm-safe { background: rgba(63,185,80,0.12); color: var(--green); }
  .otm-danger { background: rgba(248,81,73,0.12); color: var(--red); }

  .footer { text-align: center; color: var(--dim); font-size: 11px; margin-top: 20px; }
  #countdown { color: var(--accent); }
  .spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #loading { display: none; position: fixed; top: 14px; right: 20px; color: var(--dim); font-size: 12px; }

  .summary-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .summary-item { display: flex; justify-content: space-between; padding: 6px 0; }
  .summary-item .sl { color: var(--dim); font-size: 13px; }
  .summary-item .sv { font-weight: 600; font-size: 14px; }
</style>
</head>
<body>

<div id="loading"><span class="spinner"></span>loading...</div>

<div class="header">
  <h1>Option Wheel Dashboard</h1>
  <div class="sub" id="subtitle">loading...</div>
</div>

<div class="phase-bar" id="phaseBar">
  <div class="phase-step" data-phase="IDLE">IDLE<br><small>wait</small></div>
  <div class="phase-step" data-phase="SHORT_PUT">SHORT PUT<br><small>sell CSP</small></div>
  <div class="phase-step" data-phase="LONG_STOCK">LONG STOCK<br><small>assigned</small></div>
  <div class="phase-step" data-phase="SHORT_CALL">SHORT CALL<br><small>sell CC</small></div>
</div>

<div class="cards" id="cards"></div>

<div class="section" id="optionPositions">
  <h3>Option Positions</h3>
  <div id="optPosBody"></div>
</div>

<div class="section" id="openOrders">
  <h3>Open Orders</h3>
  <div id="openOrdersBody"></div>
</div>

<div class="section">
  <h3>Revenue Summary</h3>
  <div class="summary-grid" id="summaryGrid"></div>
</div>

<div class="section" id="historySection">
  <h3>Trade History</h3>
  <div id="historyBody"></div>
</div>

<div class="footer">
  Target Delta: <span id="cfgDelta"></span> &nbsp;|&nbsp;
  DTE: <span id="cfgDte"></span> &nbsp;|&nbsp;
  Contracts: <span id="cfgContracts"></span>
</div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const fmt = n => n == null ? '—' : '$' + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const pct = n => n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
const plClass = n => n >= 0 ? 'positive' : 'negative';
const phaseClass = {IDLE:'idle', SHORT_PUT:'short-put', LONG_STOCK:'long-stock', SHORT_CALL:'short-call'};

let countdown = 5;
const INTERVAL = 5;
let marketOpen = false;

setInterval(() => {
  if (!marketOpen) return;
  countdown--;
  if (countdown <= 0) { countdown = INTERVAL; fetchData(); }
  $('#countdown').textContent = countdown;
}, 1000);

async function fetchData() {
  $('#loading').style.display = 'block';
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) { console.error(d.error); return; }
    render(d);
  } catch(e) { console.error(e); }
  finally { $('#loading').style.display = 'none'; }
}

function render(d) {
  // subtitle with ET time & market status
  const mc = d.market_clock || {};
  const etTime = mc.timestamp || '—';
  const isOpen = mc.is_open;
  marketOpen = isOpen;
  const dotClass = isOpen ? 'market-open' : 'market-closed';
  const statusText = isOpen ? 'MARKET OPEN' : 'MARKET CLOSED';
  const closeInfo = isOpen && mc.next_close ? `  |  Closes: ${mc.next_close.slice(11)}` : (mc.next_open ? `  |  Next open: ${mc.next_open.slice(0,16)}` : '');
  const refreshInfo = isOpen ? `  |  Auto-refresh in <span id="countdown">${countdown}</span>s` : '  |  <span style="color:var(--dim)">No refresh outside market hours</span>';
  $('#subtitle').innerHTML = `<span class="market-dot ${dotClass}"></span>${statusText}  |  ${d.symbol} ${d.stock_price ? '$'+d.stock_price.toFixed(2) : 'N/A'}  |  ${etTime} ET${closeInfo}${refreshInfo}`;

  // phase bar
  $$('.phase-step').forEach(el => {
    el.classList.remove('active','idle','short-put','long-stock','short-call');
    if (el.dataset.phase === d.phase) {
      el.classList.add('active', phaseClass[d.phase]);
    }
  });

  // cards
  const sp = d.stock_position;
  let cardsHtml = `
    <div class="card"><div class="label">Stock Price</div><div class="value">${fmt(d.stock_price)}</div></div>
    <div class="card"><div class="label">Cash</div><div class="value">${fmt(d.account.cash)}</div></div>
    <div class="card"><div class="label">Equity</div><div class="value">${fmt(d.account.equity)}</div></div>
  `;
  if (sp) {
    cardsHtml += `<div class="card"><div class="label">${d.symbol} Stock (${sp.qty.toFixed(0)} shares)</div><div class="value ${plClass(sp.unrealized_pl)}">${fmt(sp.unrealized_pl)}<br><small>${pct(sp.unrealized_plpc)}</small></div></div>`;
  }
  const s = d.summary;
  cardsHtml += `<div class="card"><div class="label">Premium Collected</div><div class="value green">${fmt(s.total_premium)}</div></div>`;
  cardsHtml += `<div class="card"><div class="label">Realized P&L</div><div class="value ${plClass(s.realized_pl)}">${fmt(s.realized_pl)}</div></div>`;
  cardsHtml += `<div class="card"><div class="label">Rounds</div><div class="value blue">${s.rounds}</div></div>`;
  $('#cards').innerHTML = cardsHtml;

  // option positions
  if (d.option_positions.length === 0) {
    $('#optPosBody').innerHTML = '<div class="empty">No option positions</div>';
  } else {
    let html = '<table><tr><th>Contract</th><th>Type</th><th>Strike</th><th>Expiry</th><th>DTE</th><th>Qty</th><th>Cost</th><th>Current</th><th>P&L</th><th>Delta</th><th>OTM</th></tr>';
    d.option_positions.forEach(p => {
      const otmHtml = p.otm_pad != null
        ? `<span class="otm-badge ${p.otm_pad >= 0 ? 'otm-safe' : 'otm-danger'}">${p.otm_pad >= 0 ? 'OTM' : 'ITM'} $${Math.abs(p.otm_pad).toFixed(2)}</span>`
        : '—';
      html += `<tr>
        <td class="mono">${p.symbol}</td>
        <td><span class="tag tag-${p.type.toLowerCase()}">${p.type}</span></td>
        <td>${fmt(p.strike)}</td><td>${p.expiry}</td><td>${p.dte}</td>
        <td>${p.qty}</td><td>${fmt(p.cost)}</td><td>${fmt(p.current)}</td>
        <td class="${plClass(p.pl)}">${fmt(p.pl)}</td>
        <td>${p.delta != null ? p.delta.toFixed(4) : '—'}</td>
        <td>${otmHtml}</td></tr>`;
    });
    html += '</table>';
    $('#optPosBody').innerHTML = html;
  }

  // open orders
  if (d.open_orders.length === 0) {
    $('#openOrdersBody').innerHTML = '<div class="empty">No open orders</div>';
  } else {
    let html = '<table><tr><th>Contract</th><th>Type</th><th>Side</th><th>Strike</th><th>Expiry</th><th>DTE</th><th>Qty</th><th>Limit</th><th>Bid</th><th>Ask</th><th>Mid</th><th>Status</th></tr>';
    d.open_orders.forEach(o => {
      html += `<tr>
        <td class="mono">${o.symbol}</td>
        <td><span class="tag tag-${o.type.toLowerCase()}">${o.type}</span></td>
        <td><span class="tag tag-${o.side.toLowerCase()}">${o.side}</span></td>
        <td>${fmt(o.strike)}</td><td>${o.expiry}</td><td>${o.dte}</td>
        <td>${o.qty}</td><td>${o.limit_price ? fmt(o.limit_price) : '—'}</td>
        <td style="color:var(--red)">${o.bid ? fmt(o.bid) : '—'}</td>
        <td style="color:var(--green)">${o.ask ? fmt(o.ask) : '—'}</td>
        <td>${o.mid ? fmt(o.mid) : '—'}</td>
        <td><span class="status status-${o.status.toLowerCase()}">${o.status}</span></td></tr>`;
    });
    html += '</table>';
    $('#openOrdersBody').innerHTML = html;
  }

  // summary
  $('#summaryGrid').innerHTML = `
    <div class="summary-item"><span class="sl">Premium Collected (STO)</span><span class="sv positive">${fmt(s.total_premium)}</span></div>
    <div class="summary-item"><span class="sl">Buyback Cost (BTC)</span><span class="sv negative">${fmt(s.total_buyback)}</span></div>
    <div class="summary-item"><span class="sl">Realized P&L</span><span class="sv ${plClass(s.realized_pl)}">${fmt(s.realized_pl)}</span></div>
    <div class="summary-item"><span class="sl">Rounds Completed</span><span class="sv">${s.rounds}</span></div>
  `;

  // history
  if (d.history.length === 0) {
    $('#historyBody').innerHTML = '<div class="empty">No trade history</div>';
  } else {
    let html = '<table><tr><th>Time</th><th>Contract</th><th>Type</th><th>Side</th><th>Price</th><th>Status</th></tr>';
    d.history.forEach(h => {
      const statusCls = h.status === 'FILLED' ? 'filled' : h.status === 'NEW' ? 'new' : 'canceled';
      html += `<tr>
        <td class="mono" style="font-size:11px">${h.time}</td>
        <td class="mono">${h.symbol}</td>
        <td><span class="tag tag-${h.type.toLowerCase()}">${h.type}</span></td>
        <td><span class="tag tag-${h.side.toLowerCase()}">${h.side}</span></td>
        <td>${h.filled_price ? fmt(h.filled_price) : '<span style="color:var(--dim)">pending</span>'}</td>
        <td><span class="status status-${statusCls}">${h.status}</span></td></tr>`;
    });
    html += '</table>';
    $('#historyBody').innerHTML = html;
  }

  // config footer
  $('#cfgDelta').textContent = '±' + d.settings.target_delta;
  $('#cfgDte').textContent = d.settings.min_dte + '~' + d.settings.max_dte;
  $('#cfgContracts').textContent = d.settings.contracts;
}

const initData = __INIT_DATA__;
if (initData) render(initData);
fetchData();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Wheel Dashboard: http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
