"""Independent, read-only cross-session V3 pressure research.

Never joins different tape sessions into one causal segment.  The artificial
numeric segment IDs are unique across the combined in-memory dataset; neither
SQLite nor the JSONL tapes are modified.
"""
import hashlib
import json
from pathlib import Path

from dashboard.services.micro_flow_v3_dashboard_service import (
    V3DashboardError, inspect_v3_tape, existing_replay, replay_directory,
    load_buckets_jsonl,
)
from dashboard.services.micro_flow_v3_research import (
    detect_pressure_events, cluster_pressure_events, outcome_comparison,
)
from dashboard.services.micro_flow_v3_failed_aggression import (
    analyze_failed_aggression, summarize_failed_aggression,
)

MAX_SESSIONS = 4
MAX_BUCKETS = 100_000


def find_replay_for_closed_tape(root, path):
    """Accept canonical and early CLI *_1s naming; fail closed on invalid summaries.

    Source identity is preliminarily established by symbol and SQLite row count;
    a separate explicit source SHA256 verification is available in the UI.
    """
    root, path = Path(root), Path(path)
    info = inspect_v3_tape(path)
    if not info['closed']:
        return None
    pair = existing_replay(root, path, info)
    if pair:
        return (*pair, info)
    directory = replay_directory(root)
    for bucket in (directory / f'{path.stem}_1s.jsonl',):
        summary = directory / f'{path.stem}_summary.json'
        if not bucket.is_file() or not summary.is_file():
            continue
        try:
            data = json.loads(summary.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            continue
        if (data.get('schema') == 'micro-flow-v3-causal-buckets-v1'
                and data.get('symbol') == info['symbol']
                and data.get('source_tape_rows') == info['total_rows']
                and data.get('closed_source_required') is True
                and isinstance(data.get('source_tape_digest_sha256'), str)
                and isinstance(data.get('replay_jsonl_sha256'), str)):
            return bucket, summary, data, info
    return None


def hash_replay(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_replay_sessions(root, selected, *, max_sessions=MAX_SESSIONS,
                         max_buckets=MAX_BUCKETS):
    """Validate each complete source and return per-session buckets.

    Refuse missing/corrupt JSONL, duplicate sessions, symbol mismatch, wrong
    replay hash, row-count mismatch, oversized selections and overlapping time
    spans. Closed tapes only. Never silently truncate buckets.
    """
    selected = [Path(p) for p in selected]
    if not selected or len(selected) > max_sessions:
        raise V3DashboardError(f'Seleccioná entre 1 y {max_sessions} sesiones')
    if len({str(p.resolve()) for p in selected}) != len(selected):
        raise V3DashboardError('La misma sesión está seleccionada dos veces')
    sessions = []
    for tape in selected:
        pair = find_replay_for_closed_tape(root, tape)
        if pair is None:
            raise V3DashboardError(f'Tape no cerrado o sin Replay reconocido: {tape.name}')
        output, summary_path, summary, info = pair
        if not (isinstance(summary.get('replay_jsonl_sha256'), str)
                and isinstance(summary.get('source_tape_digest_sha256'), str)):
            raise V3DashboardError(f'Replay sin hashes de procedencia: {tape.name}')
        if hash_replay(output) != summary['replay_jsonl_sha256']:
            raise V3DashboardError(f'Hash JSONL incorrecto: {output.name}')
        # Preserve full-session processing; never silently cap or sample a tape.
        rows = load_buckets_jsonl(output, limit=max_buckets)
        if len(rows) != summary.get('buckets_emitted'):
            raise V3DashboardError(f'Cantidad de buckets incorrecta: {output.name}')
        if not rows:
            raise V3DashboardError(f'Replay vacío: {output.name}')
        if any(row.get('symbol') != info['symbol'] for row in rows):
            raise V3DashboardError(f'Símbolo mezclado: {output.name}')
        for row in rows:
            try:
                start = int(row['second_start_ms'])
                available = int(row['available_ms'])
                end = int(row['second_end_ms_exclusive'])
                segment = int(row['segment_id'])
            except (KeyError, TypeError, ValueError) as exc:
                raise V3DashboardError(f'Fila inválida: {output.name}') from exc
            if segment < 1 or start >= end or available < end:
                raise V3DashboardError(f'Bucket no causal: {output.name}')
        sessions.append({
            'tape': tape.name, 'tape_path': str(tape),
            'replay_path': str(output), 'summary_path': str(summary_path),
            'summary': summary, 'info': info, 'buckets': rows,
            'first_ms': min(int(r['second_start_ms']) for r in rows),
            'last_ms': max(int(r['second_start_ms']) for r in rows),
        })
    sessions.sort(key=lambda item: (item['first_ms'], item['tape']))
    if len({s['info']['symbol'] for s in sessions}) != 1:
        raise V3DashboardError('No mezclar símbolos en el mismo análisis')
    if sum(len(s['buckets']) for s in sessions) > max_buckets:
        raise V3DashboardError(f'Más de {max_buckets:,} buckets: reducir sesiones')
    for old, new in zip(sessions, sessions[1:]):
        if new['first_ms'] <= old['last_ms']:
            raise V3DashboardError('Sesiones solapadas temporalmente; revisar fuentes')
    return sessions


def isolate_segments(sessions):
    """Clone in-memory buckets and give each (tape, original_segment) its own ID.

    No signal, lookback, episode, CVD, outcome or confirmation may traverse a
    source boundary; missing clock seconds remain missing.
    """
    output, next_id = [], 1
    for session in sessions:
        local_ids = {}
        for original in session['buckets']:
            old = int(original['segment_id'])
            if old not in local_ids:
                local_ids[old] = next_id
                next_id += 1
            row = dict(original)
            row['original_segment_id'] = old
            row['segment_id'] = local_ids[old]
            row['source_session'] = session['tape']
            output.append(row)
    return output


def analyze_sessions(sessions, *, lookback=30, relative_volume=2.,
                     min_imbalance=.70, min_trades=3, cooldown_seconds=20,
                     episode_seconds=180, confirmation_seconds=30,
                     min_opposite_pct=.03, max_favorable_pct=.10,
                     min_followthrough_pct=.03, freshness_seconds=3,
                     neutral_band_pct=.01):
    buckets = isolate_segments(sessions)
    events = detect_pressure_events(
        buckets, lookback=lookback, relative_volume=relative_volume,
        min_imbalance=min_imbalance, min_trades=min_trades,
        cooldown_seconds=cooldown_seconds,
    )
    annotated, episodes = cluster_pressure_events(
        events, window_seconds=episode_seconds)
    anchors = [e for e in annotated if e['is_episode_anchor']]
    failed = analyze_failed_aggression(
        buckets, anchors, confirmation_seconds=confirmation_seconds,
        min_opposite_pct=min_opposite_pct, max_favorable_pct=max_favorable_pct,
        min_followthrough_pct=min_followthrough_pct,
        freshness_seconds=freshness_seconds,
    )
    seg_session = {r['segment_id']:r['source_session'] for r in buckets}
    for group in (annotated, episodes, failed):
        for record in group:
            record['source_session'] = seg_session[int(record['segment_id'])]
    per_session = []
    for s in sessions:
        name = s['tape']
        current_events = [r for r in annotated if r['source_session'] == name]
        current_anchors = [r for r in anchors if r['source_session'] == name]
        current_episodes = [r for r in episodes if r['source_session'] == name]
        current_failed = [r for r in failed if r['source_session'] == name]
        per_session.append({
            'Sesión': name,
            'Trades fuente': s['summary']['trades_seen'],
            'Buckets': len(s['buckets']),
            'Señales': len(current_events),
            'Episodios': len(current_episodes),
            'BUY ancla': sum(e['side']=='BUY_PRESSURE' for e in current_anchors),
            'SELL ancla': sum(e['side']=='SELL_PRESSURE' for e in current_anchors),
            'FAILED': sum(r['state']=='FAILED_AGGRESSION' for r in current_failed),
            'CONTINUED': sum(r['state']=='PRESSURE_CONTINUED' for r in current_failed),
            'UNRESOLVED': sum(r['state']=='UNRESOLVED' for r in current_failed),
            'CENSORED': sum(r['state'].startswith('CENSORED') for r in current_failed),
        })
    return {
        'buckets': buckets, 'events': annotated, 'anchors': anchors,
        'episodes': episodes, 'failed': failed, 'per_session': per_session,
        'outcome_anchor': outcome_comparison(anchors, neutral_band_pct=neutral_band_pct),
        'outcome_all': outcome_comparison(annotated, neutral_band_pct=neutral_band_pct),
        'failed_summary': summarize_failed_aggression(failed),
    }
