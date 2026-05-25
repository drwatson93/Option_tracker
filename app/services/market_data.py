import time
import yfinance as yf
from typing import Optional

_cache: dict[str, dict] = {}
CACHE_TTL = 300  # seconds


def get_price(symbol: str) -> dict:
    symbol = symbol.upper()
    cached = _cache.get(symbol)
    if cached and (time.time() - cached['ts']) < CACHE_TTL:
        return cached

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = float(info.last_price or 0)
        prev_close = float(info.previous_close or 0)
    except Exception:
        # Return stale cache if available, otherwise zeros
        if cached:
            return cached
        return {'symbol': symbol, 'price': 0.0, 'prev_close': 0.0,
                'change_pct': 0.0, 'ts': time.time(), 'error': True}

    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
    result = {
        'symbol': symbol,
        'price': price,
        'prev_close': prev_close,
        'change_pct': change_pct,
        'ts': time.time(),
        'error': False,
    }
    _cache[symbol] = result
    return result


def get_prices_bulk(symbols: list[str]) -> dict[str, dict]:
    symbols = [s.upper() for s in symbols]
    now = time.time()
    fresh = {s: _cache[s] for s in symbols if s in _cache and (now - _cache[s]['ts']) < CACHE_TTL}
    stale = [s for s in symbols if s not in fresh]

    for sym in stale:
        try:
            hist = yf.Ticker(sym).history(period='5d', auto_adjust=True)
            prices = hist['Close'].dropna()
            if prices.empty:
                raise ValueError('no price data')
            price = float(prices.iloc[-1])
            prev = float(prices.iloc[-2]) if len(prices) > 1 else price
            change_pct = ((price - prev) / prev * 100) if prev else 0.0
            result = {
                'symbol': sym, 'price': price, 'prev_close': prev,
                'change_pct': change_pct, 'ts': now, 'error': False,
            }
        except Exception as exc:
            result = {'symbol': sym, 'price': 0.0, 'prev_close': 0.0,
                      'change_pct': 0.0, 'ts': now, 'error': True,
                      'error_msg': str(exc)}
        _cache[sym] = result
        fresh[sym] = result

    return fresh


def get_price_history(symbol: str, period: str = '1y') -> list[dict]:
    """Returns OHLCV list suitable for TradingView Lightweight Charts."""
    try:
        hist = yf.Ticker(symbol.upper()).history(period=period, auto_adjust=True)
        result = []
        for ts, row in hist.iterrows():
            result.append({
                'time': ts.strftime('%Y-%m-%d'),
                'open': round(float(row['Open']), 4),
                'high': round(float(row['High']), 4),
                'low': round(float(row['Low']), 4),
                'close': round(float(row['Close']), 4),
            })
        return result
    except Exception:
        return []


def invalidate(symbol: str) -> None:
    _cache.pop(symbol.upper(), None)
