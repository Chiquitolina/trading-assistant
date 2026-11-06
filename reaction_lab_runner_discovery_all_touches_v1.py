#!/usr/bin/env python3
"""Offline, read-only Runner Discovery over ALL classified Continuous first touches.

Keeps REACTION / TOUCH_INDECISIVE / TOUCH_FAILED intact. Reuses First Touch V2
contiguous closed 1m bars and optionally Auto Paths V1 on an exact
(symbol, side, retest_ts, entry_ts) match with prefix-OHLC parity.

No writes to any SQLite, no Redis/REST, no PM2, no LIVE.
Movement hit means favorable high/low touched; a hit is NOT a TP-first trade.
Only horizons with >=H continuous bars enter a denominator; historical statuses
with missing paths remain in the output as unavailable, never as losers.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import zlib

MINUTE = 60_000
HORIZONS = (15, 30, 60, 120, 180, 360)
TARGETS = (2, 3, 5)
BAR = struct.Struct('<qdddd')
SCHEMA = 'RUNNER_DISCOVERY_ALL_FIRST_TOUCHES_V1'
STATES = ('REACTION', 'TOUCH_INDECISIVE', 'TOUCH_FAILED')
RESULT_DIR = Path('reports/reaction_lab/runner_discovery_all_touches_v1')

FIELDS = [
    'touch_key', 'symbol', 'side', 'first_touch_status', 'retest_ts', 'date_utc',
    'entry_ts', 'first_seen_ms', 'collector_state', 'collector_count',
    'path_source', 'path_count', 'path_quality', 'retest_close', 'next_open',
    'retest_mfe_180', 'retest_mae_180', 'open_mfe_180', 'open_mae_180',
]
for basis in ('retest', 'open'):
    for tp in TARGETS:
        FIELDS += [f'{basis}_time_to_run_{tp}pct', f'{basis}_tp{tp}_result', f'{basis}_tp{tp}_minute']


def ro_connect(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA query_only=ON')
    return db


def check_columns(db, table, required):
    actual = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    absent = set(required) - actual
    if absent:
        raise ValueError(f'{table}: faltan columnas {sorted(absent)}')


def decode_checked(blob, count, entry):
    """Return exact chronological 1m OHLC prefix, reject all invalid data."""
    count, entry = int(count or 0), int(entry)
    if count < 0 or count > 360:
        raise ValueError('BAD_COUNT')
    if count == 0:
        if blob:
            raise ValueError('UNEXPECTED_BLOB')
        return []
    if not blob:
        raise ValueError('MISSING_BLOB')
    raw = zlib.decompress(blob)
    if len(raw) != count * BAR.size:
        raise ValueError('COUNT_LENGTH_MISMATCH')
    output = []
    for i in range(count):
        b = BAR.unpack_from(raw, i * BAR.size)
        ts, o, hi, lo, c = b
        if (ts != entry + i*MINUTE or
                not all(math.isfinite(x) and x > 0 for x in (o, hi, lo, c)) or
                hi < max(o, lo, c) or lo > min(o, hi, c)):
            raise ValueError('INVALID_OR_GAPPED_BAR')
        output.append(b)
    return output


def same_bar(a, b):
    return a[0] == b[0] and all(math.isclose(a[i], b[i], rel_tol=1e-10, abs_tol=1e-12)
                                for i in range(1, 5))


def match_auto(ft_bars, auto_candidates, auto_db, entry):
    """Return merged 1m prefix with provenance. Reject ambiguous matches.

    Candidate metadata never identifies a first-touch status; it only identifies
    the same market path. Its original status remains the First Touch status.
    """
    if len(auto_candidates) > 1:
        return ft_bars, 'FT_ONLY_AMBIGUOUS_AUTO' if ft_bars else 'AMBIGUOUS_AUTO'
    if not auto_candidates:
        return ft_bars, 'FIRST_TOUCH_V2' if ft_bars else 'NO_PATH'
    key, candidate_entry, candidate_count = auto_candidates[0]
    if int(candidate_entry) != int(entry):
        return ft_bars, 'FT_ONLY_AUTO_ENTRY_MISMATCH' if ft_bars else 'AUTO_ENTRY_MISMATCH'
    if int(candidate_count) <= len(ft_bars):
        return ft_bars, 'FIRST_TOUCH_V2' if ft_bars else 'NO_PATH'
    record = auto_db.execute('SELECT count,path_blob FROM events WHERE event_key=?', (key,)).fetchone()
    try:
        if record is None:
            raise ValueError('MISSING_AUTO_RECORD')
        auto_bars = decode_checked(record[1], record[0], entry)
    except (ValueError, TypeError, zlib.error, struct.error):
        return ft_bars, 'FT_ONLY_BAD_AUTO' if ft_bars else 'BAD_AUTO'
    if not all(same_bar(a, b) for a,b in zip(ft_bars, auto_bars)):
        return ft_bars, 'FT_ONLY_AUTO_OHLC_CONFLICT' if ft_bars else 'AUTO_OHLC_CONFLICT'
    return auto_bars, 'AUTO_VERIFIED_EXTENSION' if ft_bars else 'AUTO_IDENTITY_VERIFIED'


def as_positive(value):
    try:
        f = float(value)
        return f if math.isfinite(f) and f > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def outcomes_for_path(bars, side, price):
    """Earliest favorable touches and independent TP-vs-1%-SL barrier sequence."""
    hits = {tp: None for tp in TARGETS}
    decisions = {tp: ('NO_HIT', None) for tp in TARGETS}
    mfe = mae = 0.0
    mfe180 = mae180 = None
    pending = set(TARGETS)
    sl_blocked = False
    for i, (_, op, high, low, close) in enumerate(bars, 1):
        if side == 'LONG':
            fav = max(0.0, 100*(high/price-1))
            adv = max(0.0, 100*(1-low/price))
        else:
            fav = max(0.0, 100*(1-low/price))
            adv = max(0.0, 100*(high/price-1))
        mfe, mae = max(mfe,fav), max(mae,adv)
        if i == 180:
            mfe180, mae180 = mfe, mae
        for tp in TARGETS:
            if hits[tp] is None and fav >= tp:
                hits[tp] = i
            if tp not in pending:
                continue
            tp_touch = fav >= tp
            sl_touch = adv >= 1.0
            if tp_touch and sl_touch:
                decisions[tp] = ('AMBIGUOUS',i)
                pending.remove(tp)
            elif tp_touch:
                decisions[tp] = ('TP_FIRST',i)
                pending.remove(tp)
            elif sl_touch:
                decisions[tp] = ('SL_FIRST',i)
                pending.remove(tp)
    return hits, decisions, mfe180, mae180


def iter_events(project, *, first_touch_db=None, auto_db=None):
    project = Path(project)
    root = project / 'reports' / 'reaction_lab'
    ft_path = Path(first_touch_db) if first_touch_db else root / 'first_touch_paths_v2.sqlite3'
    ap_path = Path(auto_db) if auto_db else root / 'auto_paths_v1.sqlite3'
    ft = ro_connect(ft_path)
    ap = ro_connect(ap_path) if ap_path.is_file() else None
    try:
        check_columns(ft, 'paths', ('touch_key','symbol','side','status','retest_ts',
                                    'entry_ts','first_seen_ms','state','count','path_blob','retest_close'))
        auto_index = defaultdict(list)
        if ap:
            check_columns(ap, 'events', ('event_key','symbol','side','retest_ts','entry_ts','count','path_blob'))
            for key,symbol,side,retest,entry,count in ap.execute(
                    'SELECT event_key,symbol,side,retest_ts,entry_ts,count FROM events'):
                auto_index[(str(symbol).upper(),str(side).upper(),int(retest))].append(
                    (key,entry,int(count)))
        source_sql = '''SELECT touch_key,symbol,side,status,retest_ts,entry_ts,
                    first_seen_ms,state,count,path_blob,retest_close
                    FROM paths ORDER BY retest_ts,touch_key'''
        for key,symbol,side,status,retest,entry,seen,state,count,blob,ref in ft.execute(source_sql):
            symbol,side,status = str(symbol).upper(),str(side).upper(),str(status).upper()
            row = dict.fromkeys(FIELDS)
            row.update(touch_key=key,symbol=symbol,side=side,first_touch_status=status,
                       retest_ts=int(retest),date_utc=datetime.fromtimestamp(int(retest)/1000,timezone.utc).date().isoformat(),
                       entry_ts=int(entry),first_seen_ms=seen,collector_state=state,
                       collector_count=int(count),retest_close=ref,path_source='NO_PATH',
                       path_count=0,path_quality='NO_CONTIGUOUS_PATH')
            if side not in ('LONG','SHORT') or int(entry) != int(retest) + MINUTE or int(entry)%MINUTE:
                row['path_quality']='BAD_IDENTITY'
                yield row
                continue
            try:
                ft_bars = decode_checked(blob,count,entry)
            except (ValueError, TypeError, zlib.error, struct.error):
                row['path_quality']='BAD_FT_PATH'
                yield row
                continue
            bars, provenance = match_auto(ft_bars, auto_index.get((symbol,side,int(retest)), []), ap, entry)
            row['path_source']=provenance
            row['path_count']=len(bars)
            if not bars:
                row['path_quality']='NO_CONTIGUOUS_PATH' if provenance in ('NO_PATH','FIRST_TOUCH_V2') else provenance
                yield row
                continue
            row['next_open']=bars[0][1]
            # Never interpret a source conflict as validated recovered data.
            row['path_quality']='CONTIGUOUS_VALID' if not any(
                value in provenance for value in ('BAD_','MISMATCH','CONFLICT','AMBIGUOUS')) else 'SOURCE_REVIEW_REQUIRED'
            for basis,price in (('retest',as_positive(ref)),('open',as_positive(bars[0][1]))):
                if price is None:
                    continue
                hits,decisions,mfe180,mae180 = outcomes_for_path(bars,side,price)
                row[f'{basis}_mfe_180']=mfe180
                row[f'{basis}_mae_180']=mae180
                for tp in TARGETS:
                    row[f'{basis}_time_to_run_{tp}pct']=hits[tp]
                    row[f'{basis}_tp{tp}_result']=decisions[tp][0]
                    row[f'{basis}_tp{tp}_minute']=decisions[tp][1]
            yield row
    finally:
        ft.close()
        if ap: ap.close()


def _atomic_write_csv(path,rows):
    counts = Counter()
    fd,tmp = tempfile.mkstemp(prefix='.all_touches_',suffix='.csv',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                counts['events']+=1
                counts[f'status_{row["first_touch_status"]}']+=1
                counts[f'quality_{row["path_quality"]}']+=1
                counts[f'provenance_{row["path_source"]}']+=1
                for h in (180,360):
                    if row['path_quality']=='CONTIGUOUS_VALID' and int(row['path_count'])>=h:
                        counts[f'evaluable_{h}']+=1
                        counts[f'evaluable_{h}_{row["first_touch_status"]}']+=1
            out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return counts


def build_dataset(project, *, first_touch_db=None, auto_db=None):
    project=Path(project).resolve()
    folder=project/RESULT_DIR
    folder.mkdir(parents=True,exist_ok=True)
    output=folder/'all_first_touches.csv'
    counts=_atomic_write_csv(output,iter_events(project,first_touch_db=first_touch_db,auto_db=auto_db))
    manifest={
        'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),
        'events_csv':str(output),'counts':dict(sorted(counts.items())),
        'settings':{'targets_pct':list(TARGETS),'horizons_min':list(HORIZONS),
                    'stop_pct_for_barrier_sequence':1.0,'evaluation_requires_full_selected_horizon':True},
        'notes':[
            'Universo: TODOS los primeros retests clasificados de First Touch V2; estado original intacto.',
            'Coincidencia Auto Paths por simbolo+lado+retest (entrada retest+1m); nunca por categoria.',
            'La tasa de runners requiere cobertura completa del horizonte elegido.',
            'Tiempo al recorrido = primer HIGH/LOW favorable; incluye runner aun si SL se toco antes.',
            'TP/SL en misma vela = AMBIGUOUS, no asumir orden de fills.',
            'Rangos historicos no implican entrada observable/ejecutable.',
            'Datos ausentes, invalidos o no maduros NO son perdedores.',
            'Las coberturas entre categorias difieren hasta completar backfill; no inferir ventaja estadistica.',
            'No se alteraron SQLite ni procesos LIVE.']}
    fd,tmp=tempfile.mkstemp(prefix='.all_manifest_',suffix='.json',dir=folder)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(manifest,out,indent=2,ensure_ascii=False)
            out.flush();os.fsync(out.fileno())
        os.replace(tmp,folder/'manifest.json')
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',default='.')
    parser.add_argument('--first-touch-db',default=None)
    parser.add_argument('--auto-db',default=None)
    args=parser.parse_args(argv)
    result=build_dataset(args.project,first_touch_db=args.first_touch_db,auto_db=args.auto_db)
    print('RUNNER_DISCOVERY_ALL_TOUCHES_V1 '+json.dumps(result,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
