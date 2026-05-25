import time
from datetime import date, timedelta
from typing import Optional

import requests
import yfinance as yf

_cache: dict[str, dict] = {}
CACHE_TTL = 300  # seconds


class _TimeoutSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        })

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', 8)
        return super().request(*args, **kwargs)


_yf_session = _TimeoutSession()


def get_price(symbol: str) -> dict:
    symbol = symbol.upper()
    cached = _cache.get(symbol)
    if cached and (time.time() - cached['ts']) < CACHE_TTL:
        return cached

    try:
        hist = yf.Ticker(symbol, session=_yf_session).history(period='5d', auto_adjust=True)
        prices = hist['Close'].dropna()
        if prices.empty:
            raise ValueError('no price data returned')
        price = float(prices.iloc[-1])
        prev = float(prices.iloc[-2]) if len(prices) > 1 else price
        change_pct = ((price - prev) / prev * 100) if prev else 0.0
        result = {
            'symbol': symbol, 'price': price, 'prev_close': prev,
            'change_pct': change_pct, 'ts': time.time(), 'error': False,
        }
    except Exception as exc:
        if cached:
            return cached
        result = {
            'symbol': symbol, 'price': 0.0, 'prev_close': 0.0,
            'change_pct': 0.0, 'ts': time.time(), 'error': True,
            'error_msg': str(exc)[:200],
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
            hist = yf.Ticker(sym, session=_yf_session).history(period='5d', auto_adjust=True)
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
            result = {
                'symbol': sym, 'price': 0.0, 'prev_close': 0.0,
                'change_pct': 0.0, 'ts': now, 'error': True,
                'error_msg': str(exc)[:200],
            }
        _cache[sym] = result
        fresh[sym] = result

    return fresh


def get_price_history(symbol: str, period: str = '1y') -> list[dict]:
    """Returns OHLCV list suitable for TradingView Lightweight Charts."""
    try:
        hist = yf.Ticker(symbol.upper(), session=_yf_session).history(period=period, auto_adjust=True)
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


def get_benchmark_returns(portfolio_start: Optional[str]) -> dict:
    """Returns % return for SPY and QQQ since portfolio_start date (YYYY-MM-DD)."""
    if not portfolio_start:
        return {}
    try:
        start = portfolio_start[:10]
        end = (date.today() + timedelta(days=1)).isoformat()
        result = {}
        for sym in ('SPY', 'QQQ'):
            hist = yf.Ticker(sym, session=_yf_session).history(
                start=start, end=end, auto_adjust=True
            )
            prices = hist['Close'].dropna()
            if len(prices) >= 2:
                first = float(prices.iloc[0])
                last = float(prices.iloc[-1])
                result[sym] = round((last - first) / first * 100, 2) if first else 0.0
        return result
    except Exception:
        return {}


def invalidate(symbol: str) -> None:
    _cache.pop(symbol.upper(), None)
