from flask import Blueprint, jsonify
from app.auth import login_required
from app.services import market_data

api_bp = Blueprint('api', __name__)


@api_bp.route('/health')
def health():
    return jsonify({'status': 'ok'})


@api_bp.route('/price/<symbol>')
@login_required
def price(symbol: str):
    data = market_data.get_price(symbol.upper())
    return jsonify(data)


@api_bp.route('/prices')
@login_required
def prices():
    from flask import request
    symbols = [s.strip().upper() for s in request.args.get('symbols', '').split(',') if s.strip()]
    if not symbols:
        return jsonify({})
    data = market_data.get_prices_bulk(symbols)
    return jsonify(data)


@api_bp.route('/benchmark')
@login_required
def benchmark():
    from flask import request as req
    start = req.args.get('start', '').strip()
    if not start:
        return jsonify({})
    data = market_data.get_benchmark_returns(start)
    return jsonify(data)


@api_bp.route('/debug')
@login_required
def debug():
    """Developer endpoint: DB counts, price fetch results, enriched position data."""
    from app.db import get_db
    from app.services.calculations import enrich_trade, enrich_position
    import traceback

    db = get_db()
    out = {}

    # ── DB counts ──
    try:
        trades_res = db.table('trades').select('*').execute()
        trades = trades_res.data or []
        positions_res = db.table('positions').select('*').execute()
        positions = positions_res.data or []
        out['db'] = {
            'trades_total': len(trades),
            'trades_open': sum(1 for t in trades if t.get('status') == 'open'),
            'trades_closed': sum(1 for t in trades if t.get('status') != 'open'),
            'positions_total': len(positions),
            'positions_open': sum(1 for p in positions if p.get('status') == 'open'),
            'positions_closed': sum(1 for p in positions if p.get('status') == 'closed'),
        }
    except Exception as e:
        out['db'] = {'error': str(e)}
        trades, positions = [], []

    # ── Price fetch test ──
    symbols = list({t['symbol'] for t in trades} | {p['symbol'] for p in positions})
    price_results = {}
    for sym in sorted(symbols):
        try:
            data = market_data.get_price(sym)
            price_results[sym] = data
        except Exception as e:
            price_results[sym] = {'error': str(e)}
    out['prices'] = price_results

    # ── Enriched positions ──
    try:
        total_open_shares: dict = {}
        for p in positions:
            if p.get('status') == 'open':
                sym = p['symbol'].upper()
                total_open_shares[sym] = total_open_shares.get(sym, 0) + int(p.get('shares') or 0)

        enriched_positions = []
        for p in positions:
            sym = p['symbol'].upper()
            price_data = price_results.get(sym, {})
            price = price_data.get('price') or None
            related = [t for t in trades if t.get('symbol', '').upper() == sym]
            total = total_open_shares.get(sym, int(p.get('shares') or 0))
            ep = enrich_position(p, price, related, total)
            enriched_positions.append({
                'symbol': ep['symbol'],
                'shares': ep['shares'],
                'status': ep['status'],
                'purchase_price': ep.get('purchase_price'),
                'current_price': ep.get('current_price'),
                'market_value': ep.get('market_value'),
                'open_pl': ep.get('open_pl'),
                'net_premiums': ep.get('net_premiums'),
                'net_premiums_per_share': ep.get('net_premiums_per_share'),
                'break_even': ep.get('break_even'),
                'net_open_pl': ep.get('net_open_pl'),
                'covered': ep.get('covered'),
            })
        out['positions'] = enriched_positions
    except Exception as e:
        out['positions'] = {'error': str(e), 'traceback': traceback.format_exc()}

    return jsonify(out)
