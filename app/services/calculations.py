from datetime import date, datetime
from typing import Optional


def _parse_date(d) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, date):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        for fmt in ('%Y-%m-%d', '%m/%d/%Y'):
            try:
                return datetime.strptime(d, fmt).date()
            except ValueError:
                continue
    return None


def dte(expiration_date) -> int:
    exp = _parse_date(expiration_date)
    if exp is None:
        return 0
    return max(0, (exp - date.today()).days)


def days_held(open_date, close_date=None) -> int:
    od = _parse_date(open_date)
    cd = _parse_date(close_date) or date.today()
    if od is None:
        return 1
    return max(1, (cd - od).days)


def itm_otm_pct(strike: float, current_price: float, option_type: str) -> Optional[float]:
    """
    Positive = ITM, Negative = OTM.
    CC/Call: stock price above strike → ITM → positive
    CSP/Put: stock price below strike → ITM → positive
    """
    if not strike or not current_price or current_price == 0:
        return None
    if option_type in ('CC', 'Call'):
        return (current_price - strike) / current_price * 100
    if option_type in ('CSP', 'Put'):
        return (strike - current_price) / current_price * 100
    return None


def net_premium_total(open_premium: float, close_premium: Optional[float],
                      quantity: int, fees: float = 0) -> float:
    """Net credit in dollars. open_premium and close_premium are per-share."""
    close = close_premium or 0.0
    return (open_premium - close) * 100 * abs(quantity) - fees


def yield_pct(net_prem: float, strike: float, quantity: int) -> Optional[float]:
    if not strike or not quantity:
        return None
    capital = strike * 100 * abs(quantity)
    if capital == 0:
        return None
    return net_prem / capital * 100


def annualized_yield(yield_p: float, held_days: int) -> float:
    return yield_p * (365 / max(1, held_days))


def extrinsic_value(option_price: float, strike: float,
                    current_price: float, option_type: str) -> float:
    if option_type in ('CC', 'Call'):
        intrinsic = max(0.0, current_price - strike)
    else:
        intrinsic = max(0.0, strike - current_price)
    return max(0.0, option_price - intrinsic)


def break_even(purchase_price: float, net_premiums_per_share: float) -> float:
    return purchase_price - net_premiums_per_share


def enrich_trade(trade: dict, current_price: Optional[float]) -> dict:
    t = dict(trade)
    strike = float(t.get('strike') or 0)
    open_prem = float(t.get('open_premium') or 0)
    close_prem = t.get('close_premium')
    close_prem_f = float(close_prem) if close_prem is not None else None
    qty = int(t.get('quantity') or 1)
    fees = float(t.get('fees') or 0)
    opt_type = t.get('option_type', '')
    exp_date = t.get('expiration_date')
    open_date = t.get('open_date')
    close_date = t.get('close_date')

    t['dte'] = dte(exp_date)
    t['days_held'] = days_held(open_date, close_date)
    t['net_premium_total'] = net_premium_total(open_prem, close_prem_f, qty, fees)
    t['current_price'] = current_price

    if current_price:
        t['itm_otm_pct'] = itm_otm_pct(strike, current_price, opt_type)
        t['price_change_pct'] = None  # populated from market_data if prev_close available
    else:
        t['itm_otm_pct'] = None
        t['price_change_pct'] = None

    y = yield_pct(t['net_premium_total'], strike, qty) if strike else None
    t['yield_pct'] = y
    t['annualized_yield'] = annualized_yield(y, t['days_held']) if y is not None else None

    # Extrinsic value only meaningful while trade is open
    if current_price and close_prem_f is None and open_prem:
        t['extrinsic_value'] = extrinsic_value(open_prem, strike, current_price, opt_type) * 100 * abs(qty)
    else:
        t['extrinsic_value'] = None

    return t


def enrich_position(position: dict, current_price: Optional[float],
                    related_trades: list) -> dict:
    p = dict(position)
    shares = int(p.get('shares') or 0)
    purchase_price = float(p.get('purchase_price') or 0)
    p['current_price'] = current_price

    # Market value
    p['market_value'] = (current_price * shares) if current_price else None

    # Open P/L
    if current_price:
        p['open_pl'] = (current_price - purchase_price) * shares
    else:
        p['open_pl'] = None

    # Net premiums collected from closed trades on this symbol
    net_prems = sum(
        net_premium_total(
            float(t.get('open_premium') or 0),
            float(t['close_premium']) if t.get('close_premium') is not None else None,
            int(t.get('quantity') or 1),
            float(t.get('fees') or 0),
        )
        for t in related_trades
    )
    p['net_premiums'] = net_prems
    p['net_premiums_per_share'] = net_prems / shares if shares else 0

    # Break-even
    p['break_even'] = break_even(purchase_price, p['net_premiums_per_share'])

    # Net open P/L = open P/L + net premiums
    p['net_open_pl'] = (p['open_pl'] + net_prems) if p['open_pl'] is not None else None

    # Coverage: does an open CC exist for this symbol?
    p['covered'] = any(
        t.get('option_type') == 'CC' and t.get('status') == 'open'
        for t in related_trades
    )

    # Last closed trade info
    closed = [t for t in related_trades if t.get('status') in ('closed', 'expired', 'assigned')]
    if closed:
        closed.sort(key=lambda t: t.get('close_date') or '', reverse=True)
        last = closed[0]
        p['last_closed_trade_date'] = last.get('close_date')
        p['last_closed_trade_type'] = last.get('option_type')
    else:
        p['last_closed_trade_date'] = None
        p['last_closed_trade_type'] = None

    return p


def portfolio_stats(open_trades: list, positions: list) -> dict:
    itm_count = sum(1 for t in open_trades if (t.get('itm_otm_pct') or 0) > 0)
    total_open = len(open_trades)

    total_net_premium = sum(t.get('net_premium_total') or 0 for t in open_trades)

    cc_capital = sum(
        float(t.get('strike') or 0) * 100 * abs(int(t.get('quantity') or 1))
        for t in open_trades if t.get('option_type') == 'CC'
    )
    put_capital = sum(
        float(t.get('strike') or 0) * 100 * abs(int(t.get('quantity') or 1))
        for t in open_trades if t.get('option_type') in ('CSP', 'Put')
    )
    open_trades_capital = cc_capital + put_capital

    positions_capital = sum(
        float(p.get('purchase_price') or 0) * int(p.get('shares') or 0)
        for p in positions if p.get('status') == 'open'
    )

    # Uncovered positions capital (positions without active CC)
    uncovered_capital = sum(
        float(p.get('market_value') or 0)
        for p in positions
        if p.get('status') == 'open' and not p.get('covered')
    )

    total_capital = open_trades_capital + positions_capital

    # Portfolio annualized yield: weighted avg
    ann_yields = [t['annualized_yield'] for t in open_trades if t.get('annualized_yield') is not None]
    avg_ann_yield = sum(ann_yields) / len(ann_yields) if ann_yields else 0

    avg_dte = 0
    dtes = [t['dte'] for t in open_trades if t.get('dte') is not None]
    avg_dte = sum(dtes) / len(dtes) if dtes else 0

    return {
        'open_trades_count': total_open,
        'itm_count': itm_count,
        'itm_pct': (itm_count / total_open * 100) if total_open else 0,
        'open_trades_net_premium': total_net_premium,
        'avg_annualized_yield': avg_ann_yield,
        'avg_dte': avg_dte,
        'open_trades_capital': open_trades_capital,
        'cc_capital': cc_capital,
        'put_capital': put_capital,
        'positions_capital': positions_capital,
        'total_capital': total_capital,
        'uncovered_capital': uncovered_capital,
    }
