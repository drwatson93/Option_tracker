import re
import io
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
    if pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d')
    s = str(value).strip()
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _parse_amount(value) -> float:
    if pd.isna(value):
        return 0.0
    s = str(value).replace('$', '').replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_option_description(desc: str):
    """Returns (symbol, expiration_date_str, option_type_raw, strike) or None."""
    m = OPTION_DESC_RE.search(str(desc))
    if not m:
        return None
    symbol = m.group(1).upper()
    exp_raw = m.group(2)
    opt_raw = m.group(3).capitalize()  # 'Call' or 'Put'
    strike = float(m.group(4))
    try:
        exp_date = datetime.strptime(exp_raw, '%m/%d/%Y').strftime('%Y-%m-%d')
    except ValueError:
        exp_date = None
    return symbol, exp_date, opt_raw, strike


def _row_to_trade(row: pd.Series) -> Optional[dict]:
    trans_code = str(row.get('Trans Code', '')).strip().upper()
    instrument = str(row.get('Instrument', '')).strip().upper()
    description = str(row.get('Description', '')).strip()
    activity_date = _parse_date(row.get('Activity Date'))
    quantity_raw = row.get('Quantity', 0)
    price_raw = row.get('Price', 0)
    amount_raw = row.get('Amount', 0)

    try:
        quantity = int(float(str(quantity_raw).replace(',', ''))) if not pd.isna(quantity_raw) else 0
    except (ValueError, TypeError):
        quantity = 0

    try:
        price = float(str(price_raw).replace('$', '').replace(',', '')) if not pd.isna(price_raw) else 0.0
    except (ValueError, TypeError):
        price = 0.0

    amount = _parse_amount(amount_raw)

    # Skip rows we can't map
    if trans_code not in ('STO', 'BTC', 'STC', 'OEXP', 'BUY', 'SELL', 'BTO'):
        return None
    if not instrument and trans_code not in ('BUY', 'SELL'):
        return None

    # --- Stock trades ---
    if trans_code in ('BUY', 'SELL'):
        # Skip if it looks like an option (description has Call/Put pattern)
        if OPTION_DESC_RE.search(description):
            return None
        return {
            'symbol': instrument or description.strip().upper(),
            'option_type': 'Stock',
            'strike': None,
            'expiration_date': None,
            'quantity': abs(quantity),
            'open_premium': price if trans_code == 'BUY' else None,
            'close_premium': price if trans_code == 'SELL' else None,
            'fees': 0,
            'open_date': activity_date,  # use activity_date for both BUY and SELL
            'close_date': activity_date if trans_code == 'SELL' else None,
            'status': 'open' if trans_code == 'BUY' else 'closed',
            'import_source': 'csv_import',
            'rh_trans_code': trans_code,
            'notes': description,
        }

    # --- Option trades ---
    parsed = _parse_option_description(description)
    if not parsed:
        return None

    sym, exp_date, opt_raw, strike = parsed
    symbol = instrument or sym

    if trans_code == 'STO':
        # Sell To Open: credit received (amount positive)
        option_type = 'CC' if opt_raw == 'Call' else 'CSP'
        premium = amount / (abs(quantity) * 100) if quantity else price
        return {
            'symbol': symbol,
            'option_type': option_type,
            'strike': strike,
            'expiration_date': exp_date,
            'quantity': abs(quantity),
            'open_premium': abs(premium),
            'close_premium': None,
            'fees': 0,
            'open_date': activity_date,
            'close_date': None,
            'status': 'open',
            'import_source': 'csv_import',
            'rh_trans_code': 'STO',
            'notes': None,
        }

    if trans_code == 'BTC':
        # Buy To Close: debit paid (amount negative)
        option_type = 'CC' if opt_raw == 'Call' else 'CSP'
        premium = abs(amount) / (abs(quantity) * 100) if quantity else price
        return {
            'symbol': symbol,
            'option_type': option_type,
            'strike': strike,
            'expiration_date': exp_date,
            'quantity': abs(quantity),
            'open_premium': None,
            'close_premium': abs(premium),
            'fees': 0,
            'open_date': activity_date,  # actual open date unknown; use activity_date as placeholder
            'close_date': activity_date,
            'status': 'closed',
            'import_source': 'csv_import',
            'rh_trans_code': 'BTC',
            'notes': None,
        }

    if trans_code == 'BTO':
        # Buy To Open long option
        opt_type_map = {'Call': 'Call', 'Put': 'Put'}
        premium = abs(amount) / (abs(quantity) * 100) if quantity else price
        return {
            'symbol': symbol,
            'option_type': opt_type_map.get(opt_raw, opt_raw),
            'strike': strike,
            'expiration_date': exp_date,
            'quantity': abs(quantity),
            'open_premium': abs(premium),
            'close_premium': None,
            'fees': 0,
            'open_date': activity_date,
            'close_date': None,
            'status': 'open',
            'import_source': 'csv_import',
            'rh_trans_code': 'BTO',
            'notes': None,
        }

    if trans_code == 'STC':
        opt_type_map = {'Call': 'Call', 'Put': 'Put'}
        premium = abs(amount) / (abs(quantity) * 100) if quantity else price
        return {
            'symbol': symbol,
            'option_type': opt_type_map.get(opt_raw, opt_raw),
            'strike': strike,
            'expiration_date': exp_date,
            'quantity': abs(quantity),
            'open_premium': None,
            'close_premium': abs(premium),
            'fees': 0,
            'open_date': activity_date,
            'close_date': activity_date,
            'status': 'closed',
            'import_source': 'csv_import',
            'rh_trans_code': 'STC',
            'notes': None,
        }

    if trans_code == 'OEXP':
        opt_type_map = {'Call': 'CC', 'Put': 'CSP'}
        return {
            'symbol': symbol,
            'option_type': opt_type_map.get(opt_raw, opt_raw),
            'strike': strike,
            'expiration_date': exp_date,
            'quantity': abs(quantity),
            'open_premium': None,
            'close_premium': 0.0,
            'fees': 0,
            'open_date': activity_date,
            'close_date': activity_date,
            'status': 'expired',
            'import_source': 'csv_import',
            'rh_trans_code': 'OEXP',
            'notes': None,
        }

    return None


def parse_robinhood_file(file_bytes: bytes, filename: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (valid_rows, error_rows).
    valid_rows: list of trade dicts ready for DB insert (after user confirmation).
    error_rows: rows that couldn't be parsed, with a 'parse_error' key.
    """
    ext = filename.rsplit('.', 1)[-1].lower()
    buf = io.BytesIO(file_bytes)

    if ext in ('xlsx', 'xls'):
        df = pd.read_excel(buf, dtype=str)
    else:
        df = pd.read_csv(buf, dtype=str)

    # Normalize column names (strip whitespace)
    df.columns = [c.strip() for c in df.columns]

    # Check for required columns
    missing = ROBINHOOD_COLUMNS - set(df.columns)
    if len(missing) > 3:
        raise ValueError(
            f"File doesn't look like a Robinhood export. Missing columns: {missing}"
        )

    valid_rows = []
    error_rows = []

    for idx, row in df.iterrows():
        try:
            trade = _row_to_trade(row)
            if trade is None:
                continue  # silently skip unrecognized rows (dividends, interest, etc.)
            valid_rows.append(trade)
        except Exception as e:
            error_rows.append({**row.to_dict(), 'parse_error': str(e)})

    return valid_rows, error_rows
