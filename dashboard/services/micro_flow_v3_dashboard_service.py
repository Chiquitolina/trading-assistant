"""Read-only Micro Flow V3 tape discovery, inspection and replay artifact selection.

Does not read legacy V1/V2 CSVs, write SQLite, or control PM2.
"""
import json
from pathlib import Path
import sqlite3


class V3DashboardError(ValueError):
    pass


def tape_directory(project_root):
    return Path(project_root) / 'reports' / 'micro_flow_v3' / 'tapes'


def replay_directory(project_root):
    return Path(project_root) / 'reports' / 'micro_flow_v3' / 'replay'


def list_v3_tapes(project_root, limit=100):
    base = tape_directory(project_root)
    if not base.is_dir():
        return []
    # Only direct symbol directories. No recursive walk into arbitrary folders.
    paths = [p for directory in base.iterdir() if directory.is_dir()
             for p in directory.glob('aggtrades_*.sqlite3') if p.is_file()]
    paths.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    return paths[:max(1, int(limit))]


def inspect_v3_tape(path):
    """Read committed WAL-aware SQLite state without changing a capture."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    # SQLite opens WAL in read-only mode; no query mutates data or tape metadata.
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        con.execute('PRAGMA query_only=ON')
        meta = dict(con.execute('SELECT key,value FROM meta').fetchall())
        if meta.get('schema_version') != '1' or not meta.get('symbol'):
            raise V3DashboardError('SQLite no es un tape V3 reconocido')
        last = con.execute('SELECT seq,kind,reason,connection,recv_wall_ns FROM tape '
                           'ORDER BY seq DESC LIMIT 1').fetchone()
        counts = dict(con.execute('SELECT kind,COUNT(*) FROM tape GROUP BY kind'))
        first_trade = con.execute('SELECT agg_id,recv_wall_ns FROM tape WHERE kind=? '
                                  'ORDER BY seq LIMIT 1', ('TRADE',)).fetchone()
        last_trade = con.execute('SELECT agg_id,recv_wall_ns,quality,connection '
                                 'FROM tape WHERE kind=? ORDER BY seq DESC LIMIT 1',
                                 ('TRADE',)).fetchone()
        anomalies = con.execute("SELECT COUNT(*) FROM tape WHERE kind='TRADE' "
                                "AND COALESCE(quality,'OK')!='OK'").fetchone()[0]
        gaps = con.execute("SELECT COUNT(*) FROM tape WHERE kind='TRADE' "
                           "AND quality LIKE '%ID_JUMP%'").fetchone()[0]
        # Recent markers are enough to inspect reconnections without growing memory.
        markers = [dict(zip(('seq','reason','connection','recv_wall_ns'), r))
                   for r in con.execute("SELECT seq,reason,connection,recv_wall_ns "
                                        "FROM tape WHERE kind='MARKER' "
                                        "ORDER BY seq DESC LIMIT 100")]
        return {
            'path': str(path), 'filename': path.name, 'symbol': meta['symbol'],
            'schema_version': meta['schema_version'],
            'provenance': meta.get('data_classification'),
            'size_bytes': path.stat().st_size,
            'mtime_ns': path.stat().st_mtime_ns,
            'closed': bool(last and last[1] == 'MARKER' and last[2] == 'PROCESS_STOP'),
            'last_reason': last[2] if last else None,
            'last_row_wall_ns': last[4] if last else None,
            'total_rows': sum(counts.values()),
            'trades': counts.get('TRADE', 0),
            'markers': counts.get('MARKER', 0),
            'anomalies': anomalies, 'id_jumps': gaps,
            'first_agg_id': first_trade[0] if first_trade else None,
            'last_agg_id': last_trade[0] if last_trade else None,
            'last_trade_wall_ns': last_trade[1] if last_trade else None,
            'last_connection': last_trade[3] if last_trade else None,
            'recent_markers': markers,
        }
    finally:
        con.close()


def replay_paths(project_root, tape_path):
    """Return canonical output paths without writing or reading an active tape."""
    tape_path = Path(tape_path)
    name = tape_path.stem
    outbase = replay_directory(project_root)
    return (outbase / (name + '_buckets_1s.jsonl'),
            outbase / (name + '_summary.json'))


def existing_replay(project_root, tape_path, tape_info):
    """Locate result for a closed tape; require matching row count and symbol.

    Naming + row count is an initial safeguard, NOT SHA256 source verification.
    """
    if not tape_info['closed']:
        return None
    tape_path = Path(tape_path)
    bucket, summary = replay_paths(project_root, tape_path)
    candidates = [(bucket, summary)]
    # Compatibility with the already generated first 10-minute pilot filenames.
    if tape_path.name == 'aggtrades_raw_10m_02.sqlite3':
        directory = replay_directory(project_root)
        candidates.append((directory / 'ethusdt_10m_02_buckets_1s.jsonl',
                           directory / 'ethusdt_10m_02_summary.json'))
    for bucket, summary in candidates:
        if not bucket.is_file() or not summary.is_file():
            continue
        try:
            details = json.loads(summary.read_text(encoding='utf-8'))
            if (details.get('schema') == 'micro-flow-v3-causal-buckets-v1'
                    and details.get('symbol') == tape_info['symbol']
                    and details.get('source_tape_rows') == tape_info['total_rows']
                    and details.get('closed_source_required') is True):
                return bucket, summary, details
        except (OSError, UnicodeError, ValueError):
            continue
    return None


def load_buckets_jsonl(path, limit=25000):
    """Strictly load a full <= 25k bucket session; never silently truncate."""
    path = Path(path)
    result = []
    with path.open('r', encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            if len(result) >= limit:
                raise V3DashboardError('Replay demasiado grande para esta vista; '
                                       'no se mostrarán resultados parciales')
            row = json.loads(line)
            if row.get('event_type') != 'BUCKET_1S':
                raise V3DashboardError('Archivo JSONL no corresponde a buckets V3')
            result.append(row)
    return result
