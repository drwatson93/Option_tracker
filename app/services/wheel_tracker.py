from datetime import date


def compute_wheel_stats(cycle: dict, trades: list[dict]) -> dict:
    """Compute summary stats for a wheel cycle given its linked trades."""
    from app.services.calculations import net_premium_total

    total_premium = 0.0
    total_pl = 0.0
    phases = []

    for t in trades:
        np = net_premium_total(
            float(t.get('open_premium') or 0),
            float(t['close_premium']) if t.get('close_premium') is not None else None,
            int(t.get('quantity') or 1),
            float(t.get('fees') or 0),
        )
        total_premium += np

        opt_type = t.get('option_type', '')
        status = t.get('status', '')

        if opt_type == 'Stock':
            # P/L from stock = (close_premium - open_premium) * shares
            open_p = float(t.get('open_premium') or 0)
            close_p = float(t.get('close_premium') or 0)
            qty = int(t.get('quantity') or 0)
            stock_pl = (close_p - open_p) * qty if (close_p and open_p) else 0
            total_pl += stock_pl
        else:
            total_pl += np

        phases.append({
            'type': opt_type,
            'status': status,
            'net_premium': np,
            'open_date': t.get('open_date'),
            'close_date': t.get('close_date'),
            'strike': t.get('strike'),
            'expiration_date': t.get('expiration_date'),
        })

    return {
        **cycle,
        'total_premium': total_premium,
        'total_pl': total_pl,
        'phases': phases,
        'trade_count': len(trades),
    }
