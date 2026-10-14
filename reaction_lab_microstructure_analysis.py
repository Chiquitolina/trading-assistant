"""Read-only, Event-ID-aligned microstructure features for Continuous V1.

The immutable telemetry_link_v1 sidecar is the authority for these factors.
Missing/late measurements never fall back to a newer REST snapshot or to
legacy deep-collector values. No SQLite writes, Redis, HTTP or future paths.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import pandas as pd

from reaction_lab_telemetry_link import SOURCES as LINK_SOURCES

SOURCE_FIELDS = {
    'oi': ('oi_contracts', 'oi_change_5m_pct', 'oi_change_15m_pct'),
    'funding': ('funding_rate',),
    'depth': ('orderbook_spread_bps', 'orderbook_imbalance_5',
              'orderbook_bid_depth_usdt', 'orderbook_ask_depth_usdt',
              'orderbook_top5_source'),
    'taker': ('taker_buy_ratio_5m', 'taker_buy_ratio_15m',
              'taker_buy_volume_5m', 'taker_sell_volume_5m',
              'taker_buy_volume_15m', 'taker_sell_volume_15m',
              'taker_delta_5m', 'taker_delta_15m', 'cvd_15m',
              'taker_source'),
    'liquidations': ('liquidations_15m_usdt',
                     'liquidations_snapshot_count_15m',
                     'liquidation_data_kind'),
}
# Required measurement(s) for claiming each frozen source is usable.
REQUIRED = {
    'oi': ('oi_contracts',),
    'funding': ('funding_rate',),
    'depth': ('orderbook_spread_bps','orderbook_imbalance_5'),
    'taker': ('taker_buy_ratio_5m','taker_buy_ratio_15m'),
    'liquidations': ('liquidations_15m_usdt','liquidations_snapshot_count_15m'),
}

SOURCE_NAMES = {
    'oi': 'Open Interest', 'funding': 'Funding',
    'depth': 'Order Book top 5', 'taker': 'Taker 5m / 15m',
    'liquidations': 'Liquidaciones observadas 15m',
}
# Reuse the frozen link's bounds rather than defining different ones here.
SOURCE_MAX_AGE_MS = {source: LINK_SOURCES[source][1]
                     for source in ('oi','funding','depth','taker')}
DERIVED = ('taker_pressure_side_5m', 'orderbook_imbalance_side_5',
           'taker_imbalance_ratio_5m', 'telemetry_max_age_sec')
MICRO_FIELDS = set(DERIVED) | {'telemetry_age_sec', 'microstructure_status'}
for _source_fields in SOURCE_FIELDS.values():
    MICRO_FIELDS.update(_source_fields)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _valid_receipt(source, q, known):
    if not isinstance(q, dict) or q.get('available') is not True:
        return False
    if source == 'liquidations':
        start = _number(q.get('window_start_ms'))
        end = _number(q.get('window_end_ms'))
        heartbeats = _number(q.get('heartbeats'))
        return (start is not None and end is not None and heartbeats is not None
                and heartbeats > 0 and start <= end <= known
                and end == known and start == known - 15 * 60_000)
    observed = _number(q.get('source_observed_ms'))
    received = _number(q.get('received_ms'))
    return (observed is not None and received is not None
            and 0 <= observed <= received <= known
            and known - observed <= SOURCE_MAX_AGE_MS[source])


def _read_link(project, pairs):
    """Return one verified link per requested Event ID; never query live state.

    The digest binds frozen features, quality and evidence. All five source
    receipts are independently qualified. Invalid digests or Event ID times
    are excluded entirely and reported via the integrity status.
    """
    db = Path(project) / 'reports/reaction_lab/telemetry_link_v1.sqlite3'
    if not db.is_file():
        return {}, False
    requested = {str(key): int(known) for key, known in pairs}
    result = {}
    with closing(sqlite3.connect(f'file:{db.resolve().as_posix()}?mode=ro', uri=True,
                                 timeout=20)) as con:
        for start in range(0, len(requested), 350):
            batch = list(requested)[start:start+350]
            if not batch:
                continue
            slots = ','.join('?' for _ in batch)
            rows = con.execute(
                'SELECT event_key,known_ts,status,features_json,quality_json,'
                f'evidence_json,evidence_sha256 FROM event_telemetry WHERE event_key IN ({slots})',
                batch,
            ).fetchall()
            for key, known, status, raw_f, raw_q, raw_e, digest in rows:
                if key in result:
                    raise ValueError('Duplicate Event ID in telemetry sidecar')
                record = {'event_key': key, 'telemetry_link_integrity': 'PASS'}
                result[key] = record
                if int(known) != requested[key]:
                    record['telemetry_link_integrity'] = 'KNOWN_TS_MISMATCH'
                    continue
                try:
                    features = json.loads(raw_f)
                    quality = json.loads(raw_q)
                    evidence = json.loads(raw_e)
                    canonical = json.dumps({'features':features,'quality':quality,
                                            'evidence':evidence}, sort_keys=True,
                                           separators=(',',':'),allow_nan=False)
                    if not isinstance(features,dict) or not isinstance(quality,dict):
                        raise ValueError('Malformed payload')
                    if hashlib.sha256(canonical.encode()).hexdigest() != digest:
                        raise ValueError('Digest mismatch')
                    if features.get('telemetry_link_known_ts') != int(known):
                        raise ValueError('Known time mismatch')
                except (ValueError,TypeError,OverflowError):
                    record['telemetry_link_integrity'] = 'INVALID_DIGEST_OR_PAYLOAD'
                    continue
                record['telemetry_link_status'] = status
                record['microstructure_status'] = 'PARTIAL'
                verified_sources = 0
                rest_ages = []
                for source, keys in SOURCE_FIELDS.items():
                    q = quality.get(source)
                    valid = _valid_receipt(source,q,int(known))
                    if valid:
                        valid = all(_number(features.get(field)) is not None
                                    for field in REQUIRED[source])
                    if valid and source == 'oi':
                        valid = _number(features['oi_contracts']) > 0
                    if valid and source == 'depth':
                        spread = _number(features['orderbook_spread_bps'])
                        imbalance = _number(features['orderbook_imbalance_5'])
                        valid = spread >= 0 and -1 <= imbalance <= 1
                    if valid and source == 'taker':
                        valid = all(0 <= _number(features[field]) <= 1
                                    for field in REQUIRED[source])
                    if valid and source == 'liquidations':
                        amount = _number(features['liquidations_15m_usdt'])
                        count = _number(features['liquidations_snapshot_count_15m'])
                        valid = amount >= 0 and count >= 0 and count.is_integer()
                    record[f'telemetry_{source}_available'] = valid
                    if not valid:
                        continue
                    # The sidecar only contains values frozen when their source
                    # was available. Never carry values from invalid sources.
                    verified_sources += 1
                    if source != 'liquidations':
                        age = (known - _number(q['source_observed_ms']))/1000.
                        record[f'telemetry_{source}_age_sec'] = age
                        rest_ages.append(age)
                    for field in keys:
                        if field in features:
                            record[field] = features[field]
                record['microstructure_status'] = ('OBSERVED' if verified_sources == len(SOURCE_FIELDS)
                                                    else 'PARTIAL' if verified_sources
                                                    else 'NOT_CAPTURED_AT_SIGNAL')
                if rest_ages:
                    record['telemetry_max_age_sec'] = max(rest_ages)
                    # Compatibility with old analyzer factor, now conservatively
                    # interpreted as maximum age of available REST sources.
                    record['telemetry_age_sec'] = max(rest_ages)
    return result, True


def enrich_runner_microstructure(frame, project):
    """Overlay independently verified event-time telemetry on the eligible cohort.

    IMPORTANT: all rows remain. Once the sidecar exists, fields from the older
    deep collector are not eligible as substitutes for missing link sources.
    """
    if frame is None or frame.empty:
        return frame.copy() if frame is not None else pd.DataFrame()
    if frame.event_key.duplicated().any():
        raise ValueError('Duplicate Event IDs in Runner Analyzer')
    if frame['known_ts'].isna().any():
        raise ValueError('Missing event known_ts')
    links, exists = _read_link(project,zip(frame.event_key,frame.known_ts))
    if not exists:
        result = frame.copy()
        result['telemetry_link_integrity'] = 'NO_SIDECAR'
        return result

    out = frame.copy()
    # Disallow fallback to less strictly qualified old deep snapshot values.
    for col in MICRO_FIELDS:
        out[col] = None
    for source in SOURCE_FIELDS:
        out[f'telemetry_{source}_available'] = False
        if source != 'liquidations':
            out[f'telemetry_{source}_age_sec'] = None
    out['telemetry_link_integrity'] = 'NOT_LINKED'
    out['telemetry_link_status'] = None
    side_by_id = dict(zip(out['event_key'].astype(str),out['signal']))
    for key, row in links.items():
        if row['telemetry_link_integrity'] != 'PASS':
            continue
        side = side_by_id[key]
        sign = 1 if side == 'LONG' else -1 if side == 'SHORT' else 0
        if sign:
            buy_ratio = _number(row.get('taker_buy_ratio_5m'))
            book = _number(row.get('orderbook_imbalance_5'))
            buy = _number(row.get('taker_buy_volume_5m'))
            sell = _number(row.get('taker_sell_volume_5m'))
            if buy_ratio is not None and 0 <= buy_ratio <= 1:
                row['taker_pressure_side_5m'] = sign*(2*buy_ratio-1)
            if book is not None and -1 <= book <= 1:
                row['orderbook_imbalance_side_5'] = sign*book
            if buy is not None and sell is not None and buy>=0 and sell>=0 and buy+sell>0:
                row['taker_imbalance_ratio_5m'] = (buy-sell)/(buy+sell)
    # Align by Event ID rather than by pandas row number / stale position.
    indexed = out.set_index('event_key',drop=False)
    for key, row in links.items():
        for field, value in row.items():
            if field != 'event_key':
                indexed.at[key,field] = value
    return indexed.reset_index(drop=True)


def source_coverage_table(frame, target=2.0):
    """Missingness by winner/control, never interpret missing as market zero."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    label = f'hit_{str(target).replace(".","_")}pct'
    if label not in frame:
        raise ValueError('Missing complete-horizon target')
    winners = frame[label].eq(True)
    rows = []
    for source, title in SOURCE_NAMES.items():
        col = f'telemetry_{source}_available'
        has = frame[col].eq(True) if col in frame else pd.Series(False,index=frame.index)
        yes = int((winners & has).sum())
        no = int((~winners & has).sum())
        with_data = int(has.sum())
        without = len(frame) - with_data
        rows.append({
            'Fuente':title,'Con dato':with_data,'Total elegible':len(frame),
            'Cobertura %':round(100*with_data/len(frame),1),
            'Runners con dato':yes,'Controles con dato':no,
            'Tasa runner con dato %':round(100*yes/with_data,2) if with_data else math.nan,
            'Tasa runner sin dato %':round(100*int((winners & ~has).sum())/without,2)
                                     if without else math.nan,
        })
    return pd.DataFrame(rows)


# Cohort-wide comparable raw percentages / dimensionless values only.
COMPARABLE_FIELDS = {
    'Cambio OI 5m (%)':'oi_change_5m_pct',
    'Cambio OI 15m (%)':'oi_change_15m_pct',
    'Funding':'funding_rate',
    'Presión taker según LONG/SHORT 5m':'taker_pressure_side_5m',
    'Desequilibrio del libro según lado':'orderbook_imbalance_side_5',
    'Desequilibrio taker 5m':'taker_imbalance_ratio_5m',
    'Spread del libro (bps)':'orderbook_spread_bps',
    'Liquidaciones observadas 15m (USDT)':'liquidations_15m_usdt',
    'Cantidad snapshots liquidaciones 15m':'liquidations_snapshot_count_15m',
}


def winner_control_microstructure(frame, target=2.0, min_each=5):
    if frame is None or frame.empty:
        return pd.DataFrame()
    label = f'hit_{str(target).replace(".","_")}pct'
    winner = frame[label].eq(True)
    rows = []
    for name, field in COMPARABLE_FIELDS.items():
        if field not in frame:
            continue
        values = pd.to_numeric(frame[field],errors='coerce')
        yes = values[winner & values.notna()]
        no = values[~winner & values.notna()]
        if min(len(yes),len(no)) < min_each:
            continue
        rows.append({
            'Factor':name,'Runners N':len(yes),'Controles N':len(no),
            'Mediana runners':round(float(yes.median()),5),
            'Mediana controles':round(float(no.median()),5),
            'Diferencia medianas':round(float(yes.median()-no.median()),5),
            'N con dato':len(yes)+len(no),
        })
    return pd.DataFrame(rows)
