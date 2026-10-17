"""Read-only reader for immutable Runner Portfolio V1 run folders.

The original LIVE sources, scanner, and portfolio engine are NEVER opened for
writing. This module does not recompute fills or balances. SQLite is authoritative.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

RUN_ROOT = Path('reports/reaction_lab/runner_portfolio_v1')
TABLES = frozenset(('decisions', 'events', 'trades', 'equity', 'path_traces'))
LABELS = {'Runner V1 Frozen': 'Runner V1 Frozen',
          'Runner Challenger 220': 'Runner Challenger 220',
          'Runner Challenger 220 · Extreme Strength Guard': 'Runner Challenger 220 · Extreme Strength Guard'}


def portfolio_root(project):
    return (Path(project).resolve() / RUN_ROOT).resolve()


def discover_runs(project):
    """List run folders without touching their contents beyond summary/manifest."""
    root = portfolio_root(project)
    if not root.is_dir():
        return []
    out = []
    for folder in sorted(root.iterdir(), reverse=True):
        if not folder.is_dir() or folder.is_symlink():
            continue
        db = folder / 'portfolio_ledger_v1.sqlite3'
        summary_file = folder / 'summary.json'
        manifest_file = folder / 'manifest.json'
        if not all(p.is_file() and not p.is_symlink()
                   for p in (db, summary_file, manifest_file)):
            continue
        try:
            summary = json.loads(summary_file.read_text(encoding='utf-8'))
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            if not isinstance(summary, dict) or not isinstance(manifest, dict):
                continue
            out.append({'run_id': folder.name,
                        'created_at_utc': manifest.get('created_at_utc'),
                        'engine_version': manifest.get('engine_version'),
                        'source_records': manifest.get('source_records'),
                        'audit_status': {k:v.get('audit_status') for k,v in summary.items()},
                        'db_mtime_ns': db.stat().st_mtime_ns})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


def resolve_run(project, run_id):
    root = portfolio_root(project)
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', run_id):
        raise ValueError('Identificador de run inválido')
    candidate = root / run_id
    if candidate.is_symlink():
        raise ValueError('No se admiten symlinks a runs')
    folder = candidate.resolve()
    if folder.parent != root or not folder.is_dir():
        raise ValueError('Run fuera del directorio permitido o inexistente')
    db = folder / 'portfolio_ledger_v1.sqlite3'
    if not db.is_file() or db.is_symlink():
        raise FileNotFoundError(f'Ledger SQLite ausente: {db}')
    return folder


def load_run_metadata(project, run_id):
    folder = resolve_run(project, run_id)
    return (json.loads((folder / 'manifest.json').read_text(encoding='utf-8')),
            json.loads((folder / 'summary.json').read_text(encoding='utf-8')))


def read_table(project, run_id, table, strategy=None):
    """Read JSON rows directly from the stored SQLite, not from source SQLite."""
    if table not in TABLES:
        raise ValueError('Tabla no autorizada')
    db = resolve_run(project, run_id) / 'portfolio_ledger_v1.sqlite3'
    # SQLite URI with mode=ro forbids an accidental write/create from this reader.
    uri = db.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as con:
        con.execute('PRAGMA query_only = ON')
        if strategy is None:
            records = con.execute(
                f'SELECT payload_json FROM "{table}" ORDER BY id').fetchall()
        else:
            records = con.execute(
                f'SELECT payload_json FROM "{table}" WHERE strategy=? ORDER BY id',
                (strategy,)).fetchall()
    rows = [json.loads(payload) for (payload,) in records]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f'Payload inválido en {table}')
    return rows


def table_counts(project, run_id):
    db = resolve_run(project, run_id) / 'portfolio_ledger_v1.sqlite3'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5) as con:
        con.execute('PRAGMA query_only = ON')
        return {name: int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in sorted(TABLES)}


def as_float(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def as_ms(value):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def active_slot_intervals(events, strategy):
    """From order placed until ORDER_EXPIRED / POSITION_EXIT event timestamp.

    POSITION_EXIT event timestamp is the close of the 1m exit bar; a trade's
    exit_ts is its OPEN, so using exit_ts would miss decisions in that last bar.
    """
    by_key = {}
    for e in events:
        if e.get('strategy') != strategy:
            continue
        key = e.get('event_key')
        if not key:
            continue
        by_key.setdefault(key, []).append(e)
    intervals = []
    for key, rows in by_key.items():
        rows = sorted(rows, key=lambda e: as_ms(e.get('timestamp_ms')) or 0)
        placed = next((as_ms(r.get('timestamp_ms')) for r in rows
                       if r.get('kind') == 'ORDER_PLACED'), None)
        settled = next((as_ms(r.get('timestamp_ms')) for r in rows
                        if r.get('kind') in ('POSITION_EXIT', 'ORDER_EXPIRED')), None)
        filled = next((as_ms(r.get('timestamp_ms')) for r in rows
                       if r.get('kind') == 'ORDER_FILLED'), None)
        if placed is not None and settled is not None and placed <= settled:
            intervals.append({'event_key': key, 'from_ms': placed, 'to_ms': settled,
                              'filled_ms': filled,
                              'release_kind': next((r.get('kind') for r in rows
                                    if as_ms(r.get('timestamp_ms')) == settled and
                                    r.get('kind') in ('POSITION_EXIT','ORDER_EXPIRED')), None)})
    return sorted(intervals, key=lambda x: x['from_ms'])


def explain_skipped_signals(decisions, events, strategy):
    slots = active_slot_intervals(events, strategy)
    out = []
    for row in decisions:
        if row.get('strategy') != strategy or row.get('decision') != 'SKIPPED_SLOT_BUSY':
            continue
        ts = as_ms(row.get('ready_ms'))
        matches = ([slot for slot in slots
                    if ts is not None and slot['from_ms'] <= ts < slot['to_ms']])
        # A single-slot wallet should always have <=1 blocker. Do not invent one.
        matches.sort(key=lambda s:s['from_ms'], reverse=True)
        blocker = matches[0] if len(matches) == 1 else None
        reason = str(row.get('reason', ''))
        reason_match = re.search(r'slot_busy_through_(\d+)', reason)
        expected_release = int(reason_match.group(1)) if reason_match else None
        out_row = dict(row)
        out_row.update({'blocker_event_key': blocker['event_key'] if blocker else None,
                        'blocker_symbol': (blocker['event_key'].split('|')[0]
                                           if blocker else None),
                        'blocker_stage': ('POSITION' if blocker and blocker['filled_ms'] is not None
                                          and blocker['filled_ms'] <= ts else 'PENDING_ORDER'
                                          if blocker else 'UNKNOWN'),
                        'blocker_until_ms': blocker['to_ms'] if blocker else None,
                        'blocker_release_matches_reason': (blocker['to_ms'] == expected_release
                                                            if blocker and expected_release is not None
                                                            else None),
                        'blocker_matches': len(matches)})
        out.append(out_row)
    return sorted(out, key=lambda x:as_ms(x.get('ready_ms')) or 0)


def verify_consistency(summary, tables):
    """Non-exhaustive accounting and integrity checks; report failures, not certify exchange fills."""
    issues = []
    trades = tables.get('trades', [])
    decisions = tables.get('decisions', [])
    events = tables.get('events', [])
    equity = tables.get('equity', [])
    by_strategy = {}
    for strategy, metrics in summary.items():
        st = [t for t in trades if t.get('strategy') == strategy]
        dec = [d for d in decisions if d.get('strategy') == strategy]
        ev = [e for e in events if e.get('strategy') == strategy]
        eq = [r for r in equity if r.get('strategy') == strategy]
        total_pnl = sum(as_float(t.get('net_pnl_usdt')) for t in st)
        initial = as_float(metrics.get('equity_initial_usdt'))
        reported_final = as_float(metrics.get('equity_final_realized_usdt'))
        if abs(initial + total_pnl - reported_final) > 1e-6:
            issues.append(f'{strategy}: PnL no coincide con equity final')
        if len(st) != int(metrics.get('resolved_trades', 0)):
            issues.append(f'{strategy}: trades no coincide con summary')
        if len([d for d in dec if d.get('decision') == 'SKIPPED_SLOT_BUSY']) != int(metrics.get('skipped_slot_busy', 0)):
            issues.append(f'{strategy}: conteo de slot busy no coincide')
        if len([d for d in dec if d.get('decision') == 'ORDER_SUBMITTED']) != int(metrics.get('orders_placed', 0)):
            issues.append(f'{strategy}: órdenes decididas no coinciden')
        if len([e for e in ev if e.get('kind') == 'POSITION_EXIT']) != len(st):
            issues.append(f'{strategy}: eventos POSITION_EXIT no coinciden con trades')
        if eq and abs(as_float(eq[-1].get('wallet_usdt')) - reported_final) > 1e-6:
            issues.append(f'{strategy}: último wallet marcado no coincide')
        blocked = explain_skipped_signals(dec, ev, strategy)
        matched = sum(1 for row in blocked if row['blocker_matches'] == 1)
        contradictory = sum(1 for row in blocked if row['blocker_release_matches_reason'] is False)
        if matched != len(blocked):
            issues.append(f'{strategy}: {len(blocked)-matched} bloqueos sin causa única')
        if contradictory:
            issues.append(f'{strategy}: {contradictory} intervalos discrepan de reason')
        by_strategy[strategy] = {'trades': len(st), 'decisions': len(dec),
                                 'events': len(ev), 'equity_marks': len(eq),
                                 'blocked_total': len(blocked), 'blocked_attributed': matched,
                                 'net_pnl_usdt': total_pnl}
    return {'issues': issues, 'by_strategy': by_strategy, 'ok': not issues}
