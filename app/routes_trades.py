import json
import os
import uuid
from datetime import date, datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, current_app, session,
)


def _save_import(data: list) -> str:
    """Write import rows to a /tmp file; return the unique ID."""
    import_id = str(uuid.uuid4())
    path = f'/tmp/owt_import_{import_id}.json'
    with open(path, 'w') as f:
        json.dump(data, f, default=str)
    return import_id


def _load_import(import_id: str) -> dict | None:
    if not import_id:
        return None
    path = f'/tmp/owt_import_{import_id}.json'
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _delete_import(import_id: str) -> None:
    path = f'/tmp/owt_import_{import_id}.json'
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _dedup_trades(candidates: list, db) -> tuple[list, int]:
    """Filter out candidates that already exist in the trades table.
    Uniqueness key: (symbol, option_type, strike, expiration_date, open_date).
    """
    if not candidates:
        return [], 0
    symbols = list({r['symbol'] for r in candidates})
    res = db.table('trades').select(
        'symbol,option_type,strike,expiration_date,open_date'
    ).in_('symbol', symbols).execute()

    def _tkey(r):
        return (
            str(r['symbol']),
            str(r['option_type']),
            round(float(r['strike'] or 0), 2),
            str(r['expiration_date'] or ''),
            str(r['open_date'] or ''),
        )

    existing = {_tkey(r) for r in (res.data or [])}
    unique, skipped = [], 0
    for row in candidates:
        if _tkey(row) in existing:
            skipped += 1
        else:
            unique.append(row)
    return unique, skipped


def _dedup_positions(candidates: list, db) -> tuple[list, int]:
    """Filter out candidates that already exist in the positions table.
    Uniqueness key: (symbol, shares, purchase_price, open_date).
    """
    if not candidates:
        return [], 0
    symbols = list({r['symbol'] for r in candidates})
    res = db.table('positions').select(
        'symbol,shares,purchase_price,open_date'
    ).in_('symbol', symbols).execute()

    def _pkey(r):
        return (
            str(r['symbol']),
            int(r['shares'] or 0),
            round(float(r['purchase_price'] or 0), 2),
            str(r['open_date'] or ''),
        )

    existing = {_pkey(r) for r in (res.data or [])}
    unique, skipped = [], 0
    for row in candidates:
        if _pkey(row) in existing:
            skipped += 1
        else:
            unique.append(row)
    return unique, skipped


from app.auth import login_required
from app.db import get_db
from app.services import market_data as md
from app.services.importer import parse_robinhood_file
from app.services.ai_import import parse_screenshot, allowed_image, AIParseError
from app.services.calculations import enrich_trade, enrich_position, portfolio_stats

trades_bp = Blueprint('trades', __name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _get_all_open_trades():
    db = get_db()
    res = db.table('trades').select('*').eq('status', 'open').order('expiration_date').execute()
    return res.data or []


def _get_all_closed_trades():
    db = get_db()
    res = db.table('trades').select('*').neq('status', 'open').order('close_date', desc=True).execute()
    return res.data or []


def _get_open_positions():
    db = get_db()
    res = db.table('positions').select('*').eq('status', 'open').order('symbol').execute()
    return res.data or []


def _enrich_trades_with_prices(trades: list) -> list:
    symbols = list({t['symbol'] for t in trades if t.get('symbol')})
    prices = md.get_prices_bulk(symbols) if symbols else {}
    enriched = []
    for t in trades:
        price_data = prices.get(t['symbol'].upper(), {})
        price = price_data.get('price') or None
        e = enrich_trade(t, price)
        e['price_change_pct'] = price_data.get('change_pct')
        enriched.append(e)
    return enriched


def _enrich_positions_with_prices(positions: list, all_trades: list) -> list:
    symbols = list({p['symbol'] for p in positions if p.get('symbol')})
    prices = md.get_prices_bulk(symbols) if symbols else {}

    # Total open shares per symbol — used to proportionally attribute premiums
    total_open_shares: dict[str, int] = {}
    for p in positions:
        if p.get('status') == 'open':
            sym = p['symbol'].upper()
            total_open_shares[sym] = total_open_shares.get(sym, 0) + int(p.get('shares') or 0)

    enriched = []
    for p in positions:
        sym = p['symbol'].upper()
        price_data = prices.get(sym, {})
        price = price_data.get('price') or None
        related = [t for t in all_trades if t.get('symbol', '').upper() == sym]
        total_shares = total_open_shares.get(sym, int(p.get('shares') or 0))
        e = enrich_position(p, price, related, total_shares)
        enriched.append(e)
    return enriched


# ──────────────────────────────────────────────
# Trades list
# ──────────────────────────────────────────────

@trades_bp.route('/')
@login_required
def index():
    open_trades_raw = _get_all_open_trades()
    closed_trades_raw = _get_all_closed_trades()
    all_trades = open_trades_raw + closed_trades_raw

    open_positions_raw = _get_open_positions()

    open_trades = _enrich_trades_with_prices(open_trades_raw)
    closed_trades = _enrich_trades_with_prices(closed_trades_raw)
    positions = _enrich_positions_with_prices(open_positions_raw, all_trades)

    stats = portfolio_stats(open_trades, positions)

    return render_template(
        'trades.html',
        open_trades=open_trades,
        closed_trades=closed_trades,
        stats=stats,
        today=date.today().isoformat(),
    )


# ──────────────────────────────────────────────
# New / Edit trade
# ──────────────────────────────────────────────

@trades_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_trade():
    prefill = {}
    if request.method == 'GET':
        # Support pre-fill from AI import via query params
        for field in ('symbol', 'option_type', 'strike', 'expiration_date',
                      'quantity', 'open_premium', 'open_date', 'notes'):
            val = request.args.get(field)
            if val:
                prefill[field] = val

    if request.method == 'POST':
        trade = _form_to_trade(request.form)
        if trade:
            get_db().table('trades').insert(trade).execute()
            flash('Trade added successfully.', 'success')
            return redirect(url_for('trades.index'))
        flash('Please fill in all required fields.', 'error')

    return render_template('trade_form.html', trade=prefill, action='new')


@trades_bp.route('/<trade_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_trade(trade_id: str):
    db = get_db()
    res = db.table('trades').select('*').eq('id', trade_id).single().execute()
    trade = res.data

    if request.method == 'POST':
        updates = _form_to_trade(request.form, allow_partial=True)
        if updates:
            db.table('trades').update(updates).eq('id', trade_id).execute()
            flash('Trade updated.', 'success')
            return redirect(url_for('trades.index'))
        flash('Please fill in all required fields.', 'error')

    return render_template('trade_form.html', trade=trade, action='edit', trade_id=trade_id)


@trades_bp.route('/<trade_id>/close', methods=['POST'])
@login_required
def close_trade(trade_id: str):
    close_premium = request.form.get('close_premium', '0')
    close_date = request.form.get('close_date') or date.today().isoformat()
    status = request.form.get('status', 'closed')

    try:
        close_premium_f = float(close_premium)
    except ValueError:
        close_premium_f = 0.0

    get_db().table('trades').update({
        'close_premium': close_premium_f,
        'close_date': close_date,
        'status': status,
    }).eq('id', trade_id).execute()

    flash('Trade closed.', 'success')
    return redirect(url_for('trades.index'))


@trades_bp.route('/<trade_id>/delete', methods=['POST'])
@login_required
def delete_trade(trade_id: str):
    get_db().table('trades').delete().eq('id', trade_id).execute()
    flash('Trade deleted.', 'success')
    return redirect(url_for('trades.index'))


def _form_to_trade(form, allow_partial: bool = False) -> dict | None:
    symbol = form.get('symbol', '').strip().upper()
    option_type = form.get('option_type', '').strip()
    open_date = form.get('open_date', '').strip() or date.today().isoformat()

    if not symbol or not option_type:
        return None

    def _float(key):
        val = form.get(key, '').strip()
        try:
            return float(val) if val else None
        except ValueError:
            return None

    def _int(key, default=1):
        val = form.get(key, '').strip()
        try:
            return int(val) if val else default
        except ValueError:
            return default

    return {
        'symbol': symbol,
        'option_type': option_type,
        'strike': _float('strike'),
        'expiration_date': form.get('expiration_date', '').strip() or None,
        'quantity': _int('quantity'),
        'open_premium': _float('open_premium'),
        'close_premium': _float('close_premium'),
        'fees': _float('fees') or 0,
        'open_date': open_date,
        'close_date': form.get('close_date', '').strip() or None,
        'status': form.get('status', 'open'),
        'import_source': 'manual',
        'notes': form.get('notes', '').strip() or None,
    }


# ──────────────────────────────────────────────
# Positions
# ──────────────────────────────────────────────

@trades_bp.route('/positions')
@login_required
def positions():
    db = get_db()
    open_pos_raw = _get_open_positions()
    closed_res = db.table('positions').select('*').eq('status', 'closed').order('close_date', desc=True).execute()
    closed_pos_raw = closed_res.data or []

    all_trades_res = db.table('trades').select('*').execute()
    all_trades = all_trades_res.data or []

    open_pos = _enrich_positions_with_prices(open_pos_raw, all_trades)
    closed_pos = _enrich_positions_with_prices(closed_pos_raw, all_trades)

    total_open_capital = sum(
        float(p.get('purchase_price') or 0) * int(p.get('shares') or 0)
        for p in open_pos_raw
    )
    open_pl_total = sum(p.get('open_pl') or 0 for p in open_pos)
    closed_pl_total = sum(
        (float(p.get('close_price') or 0) - float(p.get('purchase_price') or 0)) * int(p.get('shares') or 0)
        for p in closed_pos_raw if p.get('close_price')
    )

    covered_capital = sum(
        float(p.get('purchase_price') or 0) * int(p.get('shares') or 0)
        for p in open_pos_raw
        if any(t.get('symbol','').upper() == p.get('symbol','').upper()
               and t.get('option_type') == 'CC' and t.get('status') == 'open'
               for t in all_trades)
    )
    uncovered_capital = sum(
        float(p.get('market_value') or 0)
        for p in open_pos if not p.get('covered')
    )

    return render_template(
        'positions.html',
        open_positions=open_pos,
        closed_positions=closed_pos,
        total_open_capital=total_open_capital,
        open_pl_total=open_pl_total,
        closed_pl_total=closed_pl_total,
        covered_capital=covered_capital,
        uncovered_capital=uncovered_capital,
        today=date.today().isoformat(),
    )


@trades_bp.route('/positions/new', methods=['GET', 'POST'])
@login_required
def new_position():
    if request.method == 'POST':
        pos = _form_to_position(request.form)
        if pos:
            get_db().table('positions').insert(pos).execute()
            flash('Position added.', 'success')
            return redirect(url_for('trades.positions'))
        flash('Please fill in all required fields.', 'error')
    return render_template('position_form.html', position={}, action='new')


@trades_bp.route('/positions/<pos_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_position(pos_id: str):
    db = get_db()
    res = db.table('positions').select('*').eq('id', pos_id).single().execute()
    position = res.data

    if request.method == 'POST':
        updates = _form_to_position(request.form)
        if updates:
            db.table('positions').update(updates).eq('id', pos_id).execute()
            flash('Position updated.', 'success')
            return redirect(url_for('trades.positions'))
        flash('Please fill in all required fields.', 'error')

    return render_template('position_form.html', position=position, action='edit', pos_id=pos_id)


@trades_bp.route('/positions/<pos_id>/close', methods=['POST'])
@login_required
def close_position(pos_id: str):
    close_date = request.form.get('close_date') or date.today().isoformat()
    close_price = request.form.get('close_price', '0')
    try:
        close_price_f = float(close_price)
    except ValueError:
        close_price_f = 0.0
    get_db().table('positions').update({
        'close_date': close_date,
        'close_price': close_price_f,
        'status': 'closed',
    }).eq('id', pos_id).execute()
    flash('Position closed.', 'success')
    return redirect(url_for('trades.positions'))


@trades_bp.route('/positions/<pos_id>/delete', methods=['POST'])
@login_required
def delete_position(pos_id: str):
    get_db().table('positions').delete().eq('id', pos_id).execute()
    flash('Position deleted.', 'success')
    return redirect(url_for('trades.positions'))


def _form_to_position(form) -> dict | None:
    symbol = form.get('symbol', '').strip().upper()
    shares = form.get('shares', '').strip()
    purchase_price = form.get('purchase_price', '').strip()
    open_date = form.get('open_date', '').strip() or date.today().isoformat()

    if not symbol or not shares or not purchase_price:
        return None

    try:
        shares_i = int(shares)
        purchase_price_f = float(purchase_price)
    except ValueError:
        return None

    return {
        'symbol': symbol,
        'shares': shares_i,
        'purchase_price': purchase_price_f,
        'open_date': open_date,
        'close_date': form.get('close_date', '').strip() or None,
        'status': form.get('status', 'open'),
        'notes': form.get('notes', '').strip() or None,
    }


# ──────────────────────────────────────────────
# CSV / XLSX Import
# ──────────────────────────────────────────────

@trades_bp.route('/import/csv', methods=['GET', 'POST'])
@login_required
def import_csv():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('Please select a file.', 'error')
            return redirect(request.url)

        try:
            file_bytes = f.read()
            option_trades, positions, error_rows = parse_robinhood_file(file_bytes, f.filename)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(request.url)
        except Exception as e:
            flash(f'Error reading file: {e}', 'error')
            return redirect(request.url)

        # Store in /tmp file (too large for session cookie)
        session['import_id'] = _save_import({'option_trades': option_trades, 'positions': positions})
        return render_template(
            'csv_preview.html',
            option_trades=option_trades,
            positions=positions,
            error_rows=error_rows,
        )

    return render_template('csv_import.html')


@trades_bp.route('/import/csv/confirm', methods=['POST'])
@login_required
def import_csv_confirm():
    import_id = session.pop('import_id', None)
    data = _load_import(import_id)
    _delete_import(import_id)

    if not data:
        flash('Import session expired. Please upload again.', 'error')
        return redirect(url_for('trades.import_csv'))

    option_trades = data.get('option_trades', [])
    positions = data.get('positions', [])

    # Parse prefixed selection values: 't_N' for trades, 'p_N' for positions
    selected = request.form.getlist('selected')
    trade_indices = {int(s[2:]) for s in selected if s.startswith('t_')}
    pos_indices = {int(s[2:]) for s in selected if s.startswith('p_')}

    trades_to_insert = [t for i, t in enumerate(option_trades) if i in trade_indices]
    positions_to_insert = [p for i, p in enumerate(positions) if i in pos_indices]

    if not trades_to_insert and not positions_to_insert:
        flash('No rows selected.', 'error')
        return redirect(url_for('trades.import_csv'))

    db = get_db()
    trades_to_insert, trades_skipped = _dedup_trades(trades_to_insert, db)
    positions_to_insert, pos_skipped = _dedup_positions(positions_to_insert, db)

    batch_size = 50
    for i in range(0, len(trades_to_insert), batch_size):
        db.table('trades').insert(trades_to_insert[i:i + batch_size]).execute()

    for i in range(0, len(positions_to_insert), batch_size):
        db.table('positions').insert(positions_to_insert[i:i + batch_size]).execute()

    msg = f'Imported {len(trades_to_insert)} trade(s) and {len(positions_to_insert)} position(s).'
    skipped_total = trades_skipped + pos_skipped
    if skipped_total:
        msg += f' Skipped {skipped_total} duplicate(s) already in the database.'
    flash(msg, 'success')
    return redirect(url_for('trades.index'))


# ──────────────────────────────────────────────
# AI Screenshot Import
# ──────────────────────────────────────────────

@trades_bp.route('/import/ai', methods=['GET', 'POST'])
@login_required
def import_ai():
    if request.method == 'POST':
        f = request.files.get('screenshot')
        if not f or not f.filename:
            flash('Please upload a screenshot.', 'error')
            return redirect(request.url)

        ok, media_type = allowed_image(f.filename)
        if not ok:
            flash('Unsupported file type. Use JPG, PNG, GIF, or WebP.', 'error')
            return redirect(request.url)

        api_key = current_app.config.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            flash('AI import is not configured (missing ANTHROPIC_API_KEY).', 'error')
            return redirect(request.url)

        try:
            extracted = parse_screenshot(f.read(), media_type, api_key)
        except AIParseError as e:
            flash(f'AI could not parse the screenshot: {e}', 'error')
            extracted = {}
        except Exception as e:
            flash(f'Unexpected error during AI parsing: {e}', 'error')
            extracted = {}

        # Redirect to new trade form with pre-filled query params
        params = {k: v for k, v in extracted.items() if v is not None}
        return redirect(url_for('trades.new_trade', **params))

    return render_template('ai_import.html')


# ──────────────────────────────────────────────
# Wheels
# ──────────────────────────────────────────────

@trades_bp.route('/wheels')
@login_required
def wheels():
    db = get_db()
    cycles_res = db.table('wheel_cycles').select('*').order('created_at', desc=True).execute()
    cycles = cycles_res.data or []

    enriched = []
    for cycle in cycles:
        trades_res = db.table('trades').select('*').eq('wheel_cycle_id', cycle['id']).order('open_date').execute()
        trades = trades_res.data or []
        from app.services.wheel_tracker import compute_wheel_stats
        enriched.append(compute_wheel_stats(cycle, trades))

    return render_template('wheels.html', cycles=enriched)


@trades_bp.route('/wheels/new', methods=['POST'])
@login_required
def new_wheel():
    symbol = request.form.get('symbol', '').strip().upper()
    notes = request.form.get('notes', '').strip() or None
    if not symbol:
        flash('Symbol is required.', 'error')
        return redirect(url_for('trades.wheels'))
    get_db().table('wheel_cycles').insert({
        'symbol': symbol,
        'status': 'active',
        'started_at': date.today().isoformat(),
        'notes': notes,
    }).execute()
    flash(f'Wheel cycle for {symbol} created.', 'success')
    return redirect(url_for('trades.wheels'))


@trades_bp.route('/wheels/<cycle_id>')
@login_required
def wheel_detail(cycle_id: str):
    db = get_db()
    cycle_res = db.table('wheel_cycles').select('*').eq('id', cycle_id).single().execute()
    cycle = cycle_res.data

    trades_res = db.table('trades').select('*').eq('wheel_cycle_id', cycle_id).order('open_date').execute()
    trades = trades_res.data or []

    from app.services.wheel_tracker import compute_wheel_stats
    cycle_stats = compute_wheel_stats(cycle, trades)

    # Get unlinked trades for the same symbol to allow linking
    unlinked_res = db.table('trades').select('*').eq('symbol', cycle['symbol']).is_('wheel_cycle_id', 'null').order('open_date').execute()
    unlinked = unlinked_res.data or []

    return render_template('wheel_detail.html', cycle=cycle_stats, unlinked_trades=unlinked)


@trades_bp.route('/wheels/<cycle_id>/link', methods=['POST'])
@login_required
def wheel_link_trade(cycle_id: str):
    trade_id = request.form.get('trade_id')
    if trade_id:
        get_db().table('trades').update({'wheel_cycle_id': cycle_id}).eq('id', trade_id).execute()
        flash('Trade linked to wheel cycle.', 'success')
    return redirect(url_for('trades.wheel_detail', cycle_id=cycle_id))


@trades_bp.route('/wheels/<cycle_id>/unlink/<trade_id>', methods=['POST'])
@login_required
def wheel_unlink_trade(cycle_id: str, trade_id: str):
    get_db().table('trades').update({'wheel_cycle_id': None}).eq('id', trade_id).execute()
    flash('Trade unlinked.', 'success')
    return redirect(url_for('trades.wheel_detail', cycle_id=cycle_id))


@trades_bp.route('/wheels/<cycle_id>/complete', methods=['POST'])
@login_required
def wheel_complete(cycle_id: str):
    get_db().table('wheel_cycles').update({
        'status': 'completed',
        'completed_at': date.today().isoformat(),
    }).eq('id', cycle_id).execute()
    flash('Wheel cycle marked as completed.', 'success')
    return redirect(url_for('trades.wheels'))


@trades_bp.route('/wheels/<cycle_id>/delete', methods=['POST'])
@login_required
def wheel_delete(cycle_id: str):
    # Unlink trades first
    get_db().table('trades').update({'wheel_cycle_id': None}).eq('wheel_cycle_id', cycle_id).execute()
    get_db().table('wheel_cycles').delete().eq('id', cycle_id).execute()
    flash('Wheel cycle deleted.', 'success')
    return redirect(url_for('trades.wheels'))
