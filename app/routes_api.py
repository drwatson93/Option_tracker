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
