import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests

_cache: dict[str, dict] = {}
CACHE_TTL = 300  # seconds


class _TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', 8)
        return super().request(*args, **kwargs)


_session = _TimeoutSession()
_FINNHUB_BASE = 'https://finnhub.io/api/v1'


def _api_key() -> str:
    return os.environ.get('FINNHUB_API_KEY', '')


def _quote(symbol: str) -> dict:
    """Fetch a single quote from Finnhub. Returns enriched dict or raises."""
    key = _api_key()
    if not key:
        raise RuntimeError('FINNHUB_API_KEY not set')
    r = _session.get(f'{_FINNHUB_BASE}/quote', params={'symbol': symbol, 'token': key})
    r.raise_for_status()
    data = r.json()
    price = float(data.get('c') or 0)
    prev = float(data.get('pc') or 0)
    if price == 0:
        raise ValueError('no price data returned')
    change_pct = ((price - prev) / prev * 100) if prev else 0.0
    return {
        'symbol': symbol, 'price': price, 'prev_close': prev,
        'change_pct': round(change_pct, 4), 'ts': time.time(), 'error': False,
    }


def get_price(symbol: str) -> dict:
    symbol = symbol.upper()
    cached = _cache.get(symbol)
    if cached and (time.time() - cached['ts']) < CACHE_TTL:
        return cached
    try:
        result = _quote(symbol)
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

    if stale:
        with ThreadPoolExecutor(max_workers=min(len(stale), 5)) as pool:
            futures = {pool.submit(get_price, sym): sym for sym in stale}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    fresh[sym] = fut.result()
                except Exception as exc:
                    fresh[sym] = {
                        'symbol': sym, 'price': 0.0, 'prev_close': 0.0,
                        'change_pct': 0.0, 'ts': now, 'error': True,
                        'error_msg': str(exc)[:200],
                    }

    return fresh


def get_price_history(symbol: str, period: str = '1y') -> list[dict]:
    """Returns OHLCV list suitable for TradingView Lightweight Charts."""
    key = _api_key()
    if not key:
        return []
    period_days = {'1mo': 30, '3mo': 90, '6mo': 180, '1y': 365, '2y': 730}
    days = period_days.get(period, 365)
    now_ts = int(time.time())
    from_ts = now_ts - days * 86400
    try:
        r = _session.get(
            f'{_FINNHUB_BASE}/stock/candle',
            params={'symbol': symbol.upper(), 'resolution': 'D',
                    'from': from_ts, 'to': now_ts, 'token': key},
        )
        r.raise_for_status()
        data = r.json()
        if data.get('s') != 'ok' or not data.get('t'):
            return []
        return [
            {
                'time': datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d'),
                'open': round(float(o), 4),
                'high': round(float(h), 4),
                'low': round(float(l), 4),
                'close': round(float(c), 4),
            }
            for ts, o, h, l, c in zip(data['t'], data['o'], data['h'], data['l'], data['c'])
        ]
    except Exception:
        return []


def get_benchmark_returns(portfolio_start: Optional[str]) -> dict:
    """Returns % return for SPY and QQQ since portfolio_start (YYYY-MM-DD)."""
    key = _api_key()
    if not key or not portfolio_start:
        return {}
    try:
        start_ts = int(datetime.strptime(portfolio_start[:10], '%Y-%m-%d').timestamp())
        end_ts = int(time.time())
        result = {}
        for sym in ('SPY', 'QQQ'):
            r = _session.get(
                f'{_FINNHUB_BASE}/stock/candle',
                params={'symbol': sym, 'resolution': 'D',
                        'from': start_ts, 'to': end_ts, 'token': key},
            )
            r.raise_for_status()
            data = r.json()
            closes = data.get('c', [])
            if len(closes) >= 2:
                first, last = float(closes[0]), float(closes[-1])
                result[sym] = round((last - first) / first * 100, 2) if first else 0.0
        return result
    except Exception:
        return {}


def invalidate(symbol: str) -> None:
    _cache.pop(symbol.upper(), None)
