from collections import defaultdict
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.auth import login_required
from app.db import get_db
from app.services import market_data as md
from app.services.calculations import (
    enrich_trade, enrich_position, portfolio_stats, net_premium_total
)

views_bp = Blueprint('views', __name__)


# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────

@views_bp.route('/')
@login_required
def dashboard():
    db = get_db()
    open_trades_res = db.table('trades').select('*').eq('status', 'open').execute()
    open_trades_raw = open_trades_res.data or []

    all_trades_res = db.table('trades').select('*').execute()
    all_trades = all_trades_res.data or []

    open_pos_res = db.table('positions').select('*').eq('status', 'open').execute()
    open_positions_raw = open_pos_res.data or []

    symbols = list({t['symbol'] for t in all_trades if t.get('symbol')} |
                   {p['symbol'] for p in open_positions_raw if p.get('symbol')})
    prices = md.get_prices_bulk(symbols) if symbols else {}

    def _price(sym):
        return prices.get(sym.upper(), {}).get('price') or None

    # Total open shares per symbol for proportional premium attribution
    total_open_shares: dict = {}
    for p in open_positions_raw:
        sym = p['symbol'].upper()
        total_open_shares[sym] = total_open_shares.get(sym, 0) + int(p.get('shares') or 0)

    open_trades = [enrich_trade(t, _price(t['symbol'])) for t in open_trades_raw]
    open_positions = [
        enrich_position(
            p, _price(p['symbol']),
            [t for t in all_trades if t.get('symbol', '').upper() == p['symbol'].upper()],
            total_open_shares.get(p['symbol'].upper(), int(p.get('shares') or 0)),
        )
        for p in open_positions_raw
    ]

    stats = portfolio_stats(open_trades, open_positions)

    # Closed trade stats
    closed_trades_raw = [t for t in all_trades if t.get('status') != 'open']
    closed_enriched = [enrich_trade(t, _price(t['symbol'])) for t in closed_trades_raw]
    closed_net_premium = sum(t.get('net_premium_total') or 0 for t in closed_enriched)

    # Avg annualized yield: open trades if any, otherwise all closed trades
    if open_trades:
        display_ann_yield = stats['avg_annualized_yield']
        yield_label = 'avg open ann. yield'
    else:
        closed_yields = [t['annualized_yield'] for t in closed_enriched if t.get('annualized_yield')]
        display_ann_yield = sum(closed_yields) / len(closed_yields) if closed_yields else 0
        yield_label = 'avg closed ann. yield'

    # Upcoming expirations (next 14 days, open only)
    today = date.today()
    cutoff = today + timedelta(days=14)
    upcoming = [
        t for t in open_trades
        if t.get('expiration_date') and
        _parse_date(t['expiration_date']) is not None and
        today <= _parse_date(t['expiration_date']) <= cutoff
    ]
    upcoming.sort(key=lambda t: t['expiration_date'])

    # YTD closed P/L
    ytd_start = date(today.year, 1, 1).isoformat()
    closed_ytd = [
        t for t in all_trades
        if t.get('status') != 'open' and t.get('close_date') and t['close_date'] >= ytd_start
    ]
    ytd_pl = sum(
        net_premium_total(
            float(t.get('open_premium') or 0),
            float(t['close_premium']) if t.get('close_premium') is not None else None,
            int(t.get('quantity') or 1),
            float(t.get('fees') or 0),
        )
        for t in closed_ytd
    )

    return render_template(
        'dashboard.html',
        stats=stats,
        upcoming_expirations=upcoming,
        ytd_pl=ytd_pl,
        closed_net_premium=closed_net_premium,
        display_ann_yield=display_ann_yield,
        yield_label=yield_label,
        today=today.isoformat(),
    )


# ──────────────────────────────────────────────
# Calendar
# ──────────────────────────────────────────────

@views_bp.route('/calendar')
@login_required
def calendar():
    db = get_db()
    all_res = db.table('trades').select('*').execute()
    all_trades = all_res.data or []

    # Group all trades by expiration_date
    by_date = defaultdict(list)
    for t in all_trades:
        exp = t.get('expiration_date')
        if exp:
            by_date[str(exp)[:10]].append(t)

    symbols = list({t['symbol'] for t in all_trades if t.get('symbol')})
    prices = md.get_prices_bulk(symbols) if symbols else {}

    calendar_data = {}
    for exp_date, trades in sorted(by_date.items()):
        enriched = [
            enrich_trade(t, prices.get(t['symbol'].upper(), {}).get('price'))
            for t in trades
        ]
        net_prems = sum(e.get('net_premium_total') or 0 for e in enriched)
        open_trades_here = [t for t in trades if t.get('status') == 'open']
        capital_at_risk = sum(
            float(t.get('strike') or 0) * 100 * abs(int(t.get('quantity') or 1))
            for t in open_trades_here
            if t.get('option_type') in ('CC', 'CSP', 'Call', 'Put')
        )

        calendar_data[exp_date] = {
            'trades': enriched,
            'capital_at_risk': capital_at_risk,
            'net_premiums': net_prems,
            'trade_count': len(trades),
            'open_count': len(open_trades_here),
        }

    today = date.today()
    month = int(request.args.get('month', today.month))
    year = int(request.args.get('year', today.year))

    return render_template(
        'calendar.html',
        calendar_data=calendar_data,
        month=month,
        year=year,
        today=today.isoformat(),
    )


# ──────────────────────────────────────────────
# Ticker
# ──────────────────────────────────────────────

@views_bp.route('/ticker')
@login_required
def ticker():
    db = get_db()
    all_trades_res = db.table('trades').select('symbol').execute()
    all_pos_res = db.table('positions').select('symbol').execute()

    symbols = sorted(set(
        [t['symbol'] for t in (all_trades_res.data or []) if t.get('symbol')] +
        [p['symbol'] for p in (all_pos_res.data or []) if p.get('symbol')]
    ))

    search = request.args.get('q', '').strip().upper()
    if search:
        return redirect(url_for('views.ticker_detail', symbol=search))

    # Get prices for all ticker pills
    prices = md.get_prices_bulk(symbols) if symbols else {}

    tickers_with_state = []
    for sym in symbols:
        price_data = prices.get(sym, {})
        tickers_with_state.append({
            'symbol': sym,
            'price': price_data.get('price'),
            'change_pct': price_data.get('change_pct'),
        })

    return render_template('ticker.html', tickers=tickers_with_state)


@views_bp.route('/ticker/<symbol>')
@login_required
def ticker_detail(symbol: str):
    symbol = symbol.upper()
    db = get_db()

    trades_res = db.table('trades').select('*').eq('symbol', symbol).order('open_date').execute()
    trades_raw = trades_res.data or []

    pos_res = db.table('positions').select('*').eq('symbol', symbol).execute()
    positions_raw = pos_res.data or []

    price_data = md.get_price(symbol)
    current_price = price_data.get('price')

    period = request.args.get('period', '6M').upper()
    period_map = {'1M': '1mo', '3M': '3mo', '6M': '6mo', '1Y': '1y', '2Y': '2y'}
    yf_period = period_map.get(period, '6mo')
    price_history = md.get_price_history(symbol, yf_period)

    enriched_trades = [enrich_trade(t, current_price) for t in trades_raw]
    open_trades = [t for t in enriched_trades if t.get('status') == 'open']
    closed_trades = [t for t in enriched_trades if t.get('status') != 'open']

    total_open_shares = 0
    for p in positions_raw:
        if p.get('status') == 'open':
            total_open_shares += int(p.get('shares') or 0)

    enriched_positions = [
        enrich_position(p, current_price, trades_raw, total_open_shares)
        for p in positions_raw
    ]

    # Per-ticker P/L summary
    net_premiums_total = sum(
        net_premium_total(
            float(t.get('open_premium') or 0),
            float(t['close_premium']) if t.get('close_premium') is not None else None,
            int(t.get('quantity') or 1),
            float(t.get('fees') or 0),
        )
        for t in trades_raw
    )
    open_pl = sum(p.get('open_pl') or 0 for p in enriched_positions if p.get('status') == 'open')
    net_pl = open_pl + net_premiums_total

    closed_count = len(closed_trades)
    profitable = len([t for t in closed_trades if (t.get('net_premium_total') or 0) > 0])
    win_pct = (profitable / closed_count * 100) if closed_count else None

    return render_template(
        'ticker_detail.html',
        symbol=symbol,
        price_data=price_data,
        price_history=price_history,
        open_trades=open_trades,
        closed_trades=closed_trades,
        positions=enriched_positions,
        net_premiums_total=net_premiums_total,
        open_pl=open_pl,
        net_pl=net_pl,
        win_pct=win_pct,
        period=period,
        periods=['1M', '3M', '6M', '1Y', '2Y'],
    )


def _parse_date(d):
    if d is None:
        return None
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.strptime(d[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None
