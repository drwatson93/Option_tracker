import re
import io
from collections import defaultdict
from datetime import datetime
from typing import Optional
import pandas as pd

OPTION_DESC_RE = re.compile(
    r'([A-Z]+)\s+(\d{1,2}/\d{1,2}/\d{4})\s+(Call|Put)\s+\$?([\d.]+)',
    re.IGNORECASE,
)

ROBINHOOD_COLUMNS = {
    'Activity Date', 'Process Date', 'Settle Date',
    'Instrument', 'Description', 'Trans Code', 'Quantity', 'Price', 'Amount',
}


def _parse_date(value) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    s = str(value).strip()
    if not s or s.lower() in ('nan', 'nat', 'none', ''):
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _parse_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(str(value).replace('$', '').replace(',', '').strip())
    except (ValueError, TypeError):
        return None


def _parse_int(value, default=0) -> int:
    f = _parse_float(value)
    return int(f) if f is not None else default


def _parse_option_description(desc: str) -> Optional[tuple]:
    """Returns (symbol, exp_date_str, 'Call'/'Put', strike_float) or None."""
    m = OPTION_DESC_RE.search(str(desc))
    if not m:
        return None
    symbol = m.group(1).upper()
    opt_raw = m.group(3).capitalize()
    strike = float(m.group(4))
    try:
        exp_date = datetime.strptime(m.group(2), '%m/%d/%Y').strftime('%Y-%m-%d')
    except ValueError:
        return None
    return symbol, exp_date, opt_raw, strike


# ──────────────────────────────────────────────
# Option grouping and merging
# ──────────────────────────────────────────────

OPTION_OPEN_CODES = {'STO', 'BTO'}
OPTION_CLOSE_CODES = {'BTC', 'STC', 'OEXP', 'OASGN'}
OPTION_CODES = OPTION_OPEN_CODES | OPTION_CLOSE_CODES


def _process_option_groups(option_rows: list) -> list:
    """
    Groups option transactions by (symbol, strike, exp_date, Call/Put) and
    merges each group into a single trade record with correct net P/L.
    """
    groups: dict = defaultdict(lambda: {'opens': [], 'closes': [], 'meta': None})

    for row in option_rows:
        parsed = _parse_option_description(row['description'])
        if not parsed:
            continue
        sym, exp_date, opt_raw, strike = parsed
        key = (sym, round(strike, 2), exp_date, opt_raw)
        g = groups[key]
        g['meta'] = {'symbol': sym, 'strike': strike, 'exp': exp_date, 'opt_raw': opt_raw}

        tc = row['trans_code']
        if tc in OPTION_OPEN_CODES:
            g['opens'].append(row)
        else:
            g['closes'].append(row)

    trades = []
    for key, g in groups.items():
        if g['meta'] is None:
            continue
        meta = g['meta']
        opens = g['opens']
        closes = g['closes']

        # Determine option type (CC/CSP for short, Call/Put for long)
        is_short = any(r['trans_code'] == 'STO' for r in opens) or (
            not opens and any(r['trans_code'] in ('BTC', 'OEXP', 'OASGN') for r in closes)
        )
        if is_short:
            opt_type = 'CC' if meta['opt_raw'] == 'Call' else 'CSP'
        else:
            opt_type = meta['opt_raw']  # 'Call' or 'Put'

        # Total open quantity (use closes qty if no opens found)
        total_open_qty = sum(_parse_int(r['quantity']) for r in opens)
        if total_open_qty == 0:
            total_open_qty = sum(_parse_int(r['quantity']) for r in closes
                                 if r['trans_code'] not in ('OEXP', 'OASGN'))
        if total_open_qty == 0:
            total_open_qty = 1

        # Dollar amounts (all stored as signed floats — STO positive, BTC negative)
        open_total_dollars = sum(
            abs(r['amount']) for r in opens
            if r['amount'] is not None and not (isinstance(r['amount'], float) and pd.isna(r['amount']))
        )
        close_total_dollars = sum(
            abs(r['amount']) for r in closes
            if r['amount'] is not None and not (isinstance(r['amount'], float) and pd.isna(r['amount']))
        )

        open_prem = open_total_dollars / (total_open_qty * 100) if open_total_dollars else None
        close_prem = close_total_dollars / (total_open_qty * 100) if close_total_dollars else None

        # Dates
        open_dates = [r['activity_date'] for r in opens if r['activity_date']]
        close_dates = [r['activity_date'] for r in closes if r['activity_date']]
        open_date = min(open_dates) if open_dates else (min(close_dates) if close_dates else None)
        close_date = max(close_dates) if close_dates else None

        # Status
        if not closes:
            status = 'open'
            close_prem = None
            close_date = None
        elif any(r['trans_code'] == 'OASGN' for r in closes):
            status = 'assigned'
        elif any(r['trans_code'] == 'OEXP' for r in closes):
            status = 'expired'
        else:
            status = 'closed'

        trades.append({
            'symbol': meta['symbol'],
            'option_type': opt_type,
            'strike': meta['strike'],
            'expiration_date': meta['exp'],
            'quantity': total_open_qty,
            'open_premium': round(open_prem, 4) if open_prem else None,
            'close_premium': round(close_prem, 4) if close_prem else None,
            'fees': 0,
            'open_date': open_date,
            'close_date': close_date,
            'status': status,
            'import_source': 'csv_import',
            'rh_trans_code': 'STO' if is_short else 'BTO',
            'notes': None,
        })

    return trades


# ──────────────────────────────────────────────
# Stock FIFO matching
# ──────────────────────────────────────────────

def _process_stocks(stock_rows: list) -> list:
    """
    FIFO matching of Buy lots against Sell transactions.
    Returns both open lots (still held) and closed lots (sold), so
    assignment-triggered sells appear in the Positions closed tab.
    """
    sorted_rows = sorted(stock_rows, key=lambda r: r['activity_date'] or '0000-01-01')

    inventory: dict = defaultdict(list)  # symbol -> [{qty, price, date}]
    closed: list = []

    for row in sorted_rows:
        sym = row['symbol']
        qty = _parse_int(row['quantity'])
        sell_price = row['price'] or 0.0
        sell_date = row['activity_date']
        tc = row['trans_code'].upper()

        if tc == 'BUY':
            inventory[sym].append({'qty': qty, 'price': sell_price, 'date': sell_date})
        elif tc == 'SELL':
            remaining = qty
            while remaining > 0 and inventory[sym]:
                lot = inventory[sym][0]
                filled = min(lot['qty'], remaining)
                closed.append({
                    'symbol': sym,
                    'shares': filled,
                    'purchase_price': lot['price'],
                    'close_price': sell_price,
                    'open_date': lot['date'],
                    'close_date': sell_date,
                    'status': 'closed',
                    'notes': None,
                })
                remaining -= filled
                if filled >= lot['qty']:
                    inventory[sym].pop(0)
                else:
                    lot['qty'] -= filled

    open_positions = []
    for sym, lots in inventory.items():
        for lot in lots:
            if lot['qty'] > 0:
                open_positions.append({
                    'symbol': sym,
                    'shares': lot['qty'],
                    'purchase_price': lot['price'],
                    'close_price': None,
                    'open_date': lot['date'],
                    'close_date': None,
                    'status': 'open',
                    'notes': None,
                })

    return open_positions + closed


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

def parse_robinhood_file(file_bytes: bytes, filename: str) -> tuple[list, list, list]:
    """
    Returns (option_trades, positions, error_rows).
    option_trades: merged option records (STO+BTC pairs → single trade with net P/L)
    positions: open stock lots (FIFO-matched BUY - SELL)
    error_rows: rows that could not be parsed
    """
    ext = filename.rsplit('.', 1)[-1].lower()
    buf = io.BytesIO(file_bytes)

    if ext in ('xlsx', 'xls'):
        df = pd.read_excel(buf)
    else:
        df = pd.read_csv(buf, dtype=str)

    df.columns = [c.strip() for c in df.columns]

    missing = ROBINHOOD_COLUMNS - set(df.columns)
    if len(missing) > 3:
        raise ValueError(
            f"File doesn't look like a Robinhood export. Missing columns: {missing}"
        )

    option_rows = []
    stock_rows = []
    error_rows = []

    for _, row in df.iterrows():
        tc = str(row.get('Trans Code', '')).strip().upper()
        desc = str(row.get('Description', '')).strip()
        instrument = str(row.get('Instrument', '')).strip().upper()

        try:
            base = {
                'trans_code': tc,
                'symbol': instrument,
                'description': desc,
                'activity_date': _parse_date(row.get('Activity Date')),
                'quantity': _parse_int(row.get('Quantity'), default=1),
                'price': _parse_float(row.get('Price')),
                'amount': _parse_float(row.get('Amount')),
            }

            if tc in OPTION_CODES:
                option_rows.append(base)
            elif tc in ('BUY', 'SELL'):
                # Skip if description looks like an option
                if OPTION_DESC_RE.search(desc):
                    continue
                stock_rows.append(base)
            # else: dividend, interest, journal, etc. — silently skip

        except Exception as e:
            error_rows.append({**row.to_dict(), 'parse_error': str(e)})

    option_trades = _process_option_groups(option_rows)
    positions = _process_stocks(stock_rows)

    return option_trades, positions, error_rows
