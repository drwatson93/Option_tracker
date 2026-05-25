from collections import defaultdict
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.auth import login_required
from app.db import get_db
from app.services import market_data as md
from app.services.calculations import (
    enrich_trade, enrich_position, portfolio_stats, net_premium_total,
    days_held, yield_pct, annualized_yield,
)

views_bp = Blueprint('views', __name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _monthly_earnings(all_trades: list, n: int = 6) -> list[dict]:
    """Net premium collected per calendar month for the last n months."""
    today = date.today()
    month_earnings: dict = {}
    for t in all_trades:
        if t.get('status') != 'open' and t.get('close_date'):
            key = str(t['close_date'])[:7]  # YYYY-MM
            npt = net_premium_total(
                float(t.get('open_premium') or 0),
                float(t['close_premium']) if t.get('close_premium') is not None else None,
                int(t.get('quantity') or 1),
                float(t.get('fees') or 0),
            )
            month_earnings[key] = month_earnings.get(key, 0.0) + npt

    result = []
    for i in range(n - 1, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        key = f'{y}-{m:02d}'
        result.append({
            'key': key,
            'label': date(y, m, 1).strftime('%b %Y'),
            'earnings': round(month_earnings.get(key, 0.0), 2),
        })
    return result


def _per_symbol_stats(all_trades: list) -> dict:
    """Aggregated per-symbol stats from all trades."""
    stats: dict = {}
    for t in all_trades:
        sym = (t.get('symbol') or '').upper()
        if not sym:
            continue
        if sym not in stats:
            stats[sym] = {
                'symbol': sym, 'trade_count': 0, 'contracts': 0,
                'dte_days': 0, 'net_premium': 0.0,
                'ann_yield_sum': 0.0, 'ann_yield_count': 0,
                'position_pl': 0.0,
            }
        npt = net_premium_total(
            float(t.get('open_premium') or 0),
            float(t['close_premium']) if t.get('close_premium') is not None else None,
            int(t.get('quantity') or 1),
            float(t.get('fees') or 0),
        )
        qty = abs(int(t.get('quantity') or 1))
        dh = days_held(t.get('open_date'), t.get('close_date'))
        stats[sym]['trade_count'] += 1
        stats[sym]['contracts'] += qty
        stats[sym]['net_premium'] += npt
        stats[sym]['dte_days'] += dh
        strike = float(t.get('strike') or 0)
        if strike:
            yp = yield_pct(npt, strike, qty)
            if yp is not None:
                ay = annualized_yield(yp, dh)
                stats[sym]['ann_yield_sum'] += ay
                stats[sym]['ann_yield_count'] += 1

    for s in stats.values():
        s['avg_dte'] = round(s['dte_days'] / s['trade_count'], 1) if s['trade_count'] else 0
        s['avg_ann_yield'] = round(
            s['ann_yield_sum'] / s['ann_yield_count'], 2
        ) if s['ann_yield_count'] else 0
        s['net_pl'] = s['net_premium']  # updated below with position P/L

    return stats


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

    # All-time premium collected (open + closed)
    premium_collected = closed_net_premium + stats['open_trades_net_premium']

    # Open position unrealized P/L
    open_pos_pl = sum(p.get('open_pl') or 0 for p in open_positions)

    # Total net P/L = all premiums + unrealized position gains/losses
    total_net_pl = premium_collected + open_pos_pl

    # Return % on currently deployed capital
    return_pct = (total_net_pl / stats['total_capital'] * 100) if stats['total_capital'] else 0.0

    # Avg annualized yield label
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

    # Monthly earnings (last 6 months)
    monthly_earnings = _monthly_earnings(all_trades)
    monthly_max = max((abs(m['earnings']) for m in monthly_earnings), default=1) or 1

    # Per-symbol stats for leaders/laggards tables
    sym_stats = _per_symbol_stats(all_trades)
    for p in open_positions:
        sym = p['symbol'].upper()
        pl = p.get('open_pl') or 0
        if sym in sym_stats:
            sym_stats[sym]['position_pl'] += pl
            sym_stats[sym]['net_pl'] = sym_stats[sym]['net_premium'] + sym_stats[sym]['position_pl']
        else:
            sym_stats[sym] = {
                'symbol': sym, 'trade_count': 0, 'contracts': 0,
                'avg_dte': 0, 'net_premium': 0.0, 'avg_ann_yield': 0,
                'position_pl': pl, 'net_pl': pl,
            }

    leaders_by_premium = sorted(
        [s for s in sym_stats.values() if s['trade_count'] > 0],
        key=lambda x: x['net_premium'], reverse=True
    )[:5]
    all_sym_pl = sorted(sym_stats.values(), key=lambda x: x['net_pl'], reverse=True)
    leaders_by_pl = all_sym_pl[:5]
    laggards_by_pl = list(reversed(all_sym_pl))[:5]

    # Portfolio start date for benchmark reference
    open_dates = [t.get('open_date') for t in all_trades if t.get('open_date')]
    portfolio_start = min(open_dates)[:10] if open_dates else None

    return render_template(
        'dashboard.html',
        stats=stats,
        upcoming_expirations=upcoming,
        ytd_pl=ytd_pl,
        closed_net_premium=closed_net_premium,
        premium_collected=premium_collected,
        open_pos_pl=open_pos_pl,
        total_net_pl=total_net_pl,
        return_pct=return_pct,
        display_ann_yield=display_ann_yield,
        yield_label=yield_label,
        monthly_earnings=monthly_earnings,
        monthly_max=monthly_max,
        leaders_by_premium=leaders_by_premium,
        leaders_by_pl=leaders_by_pl,
        laggards_by_pl=laggards_by_pl,
        portfolio_start=portfolio_start,
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

        # Strip fields that can contain large/problematic strings from trade dicts
        safe_trades = []
        for e in enriched:
            safe_trades.append({
                'id': e.get('id'),
                'symbol': e.get('symbol'),
                'option_type': e.get('option_type'),
                'strike': e.get('strike'),
                'status': e.get('status'),
                'quantity': e.get('quantity'),
                'current_price': e.get('current_price'),
                'net_premium_total': e.get('net_premium_total'),
            })

        calendar_data[exp_date] = {
            'trades': safe_trades,
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
    current_price = price_data.get('price') or None

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

    # Aggregate all open position lots into a single summary card
    open_pos_list = [p for p in enriched_positions if p.get('status') == 'open']
    open_position_summary = None
    if open_pos_list:
        total_shares_agg = sum(int(p.get('shares') or 0) for p in open_pos_list)
        total_cost = sum(
            float(p.get('purchase_price') or 0) * int(p.get('shares') or 0)
            for p in open_pos_list
        )
        avg_price_agg = total_cost / total_shares_agg if total_shares_agg else 0
        total_open_pl = sum(p.get('open_pl') or 0 for p in open_pos_list)
        total_net_premiums_pos = sum(p.get('net_premiums') or 0 for p in open_pos_list)
        total_net_open_pl = sum(p.get('net_open_pl') or 0 for p in open_pos_list)
        open_position_summary = {
            'shares': total_shares_agg,
            'avg_purchase_price': avg_price_agg,
            'open_pl': total_open_pl if current_price else None,
            'net_premiums': total_net_premiums_pos,
            'net_open_pl': total_net_open_pl if current_price else None,
            'lot_count': len(open_pos_list),
        }

    return render_template(
        'ticker_detail.html',
        symbol=symbol,
        price_data=price_data,
        price_history=price_history,
        open_trades=open_trades,
        closed_trades=closed_trades,
        positions=enriched_positions,
        open_position_summary=open_position_summary,
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
