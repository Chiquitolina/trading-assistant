"""Fail-closed historical bridge: structural replay + externally reconstructed causal factors.

NEVER treats replay virtual first_seen as actual LIVE first_seen. Historical factors must
be independently derived from closed historic candles and contain their as-of timestamps.
Existing scanner/replay source databases are never written to. No Binance calls here.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MINUTE = 60_000
# A candle closes at its boundary; no OHLC fill can occur at the SAME exact
# millisecond that the required candle-close information becomes known.
POST_BOUNDARY_GUARD_MS = 1
REQUIRED_FACTOR_FIELDS = (
    'event_key', 'room_1h_pct', 'volume_rank_pct_4h', 'return_rank_pct_4h',
    'room_available_ms', 'ranks_available_ms', 'room_asof_ms', 'ranks_asof_ms',
    'room_method', 'ranks_method',
)
# These are historical derivation provenance, not guarantees of parity.
ALLOWED_METHODS = {'ASOF_CONFIRMED_SWINGS_1H', 'ASOF_CLOSED_4H_CROSS_SECTION'}


def load_csv(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f'Empty or invalid CSV: {path}')
        return list(reader)


def as_int(v):
    try:
        if v is None or v == '' or isinstance(v, bool): return None
        n = int(v)
        return n if str(n) == str(v).strip() or isinstance(v, int) else None
    except (ValueError, TypeError, OverflowError):
        return None


def as_float(v):
    try:
        if v is None or v == '' or isinstance(v, bool): return None
        n = float(v)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def load_replay(path, summary_path=None):
    """Require stable Event IDs and genuine replay structure; fail on duplicate conflicts."""
    rows = load_csv(path)
    required = {'event_key','symbol','side','retest_ts','known_ts','first_replay_observed_ms',
                'observation_type','source_hash','causal_json'}
    if rows and not required.issubset(rows[0]):
        raise ValueError(f'Missing replay fields: {sorted(required-set(rows[0]))}')
    summary = {}
    if summary_path:
        summary = json.loads(Path(summary_path).read_text(encoding='utf-8'))
        # Do not hide partial source coverage in preflight. Actual RUN is blocked
        # unless explicit --allow-partial was passed.
        if summary.get('mode') not in ('gap','parity','research'):
            raise ValueError('Unsupported replay mode; expected canonical replay summary')
    dedup = {}
    for r in rows:
        key = r['event_key']
        ts = as_int(r.get('retest_ts'))
        known = as_int(r.get('known_ts'))
        observed = as_int(r.get('first_replay_observed_ms'))
        if ts is None or known != ts+MINUTE or observed is None or observed < known:
            raise ValueError(f'Impossible causal timestamps: {key}')
        if f"{r['symbol']}|{r['side']}|{ts}" != key:
            raise ValueError(f'Event ID mismatch: {key}')
        if r['side'] not in ('LONG','SHORT') or r.get('observation_type') != 'HISTORICAL_REPLAY_UNVERIFIED':
            raise ValueError(f'Unexpected event provenance: {key}')
        try:
            causal = json.loads(r['causal_json'])
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Malformed structural payload {key}') from exc
        dep = as_float(causal.get('max_departure_pct'))
        if dep is None or dep < 0 or as_int(causal.get('retest_timestamp')) != ts:
            raise ValueError(f'Invalid departure/timestamp {key}')
        if key in dedup and dedup[key] != r:
            raise ValueError(f'Conflicting duplicate {key}')
        dedup[key] = r
    if summary and summary.get('canonical_hash'):
        if any(r['source_hash'] != summary['canonical_hash'] for r in dedup.values()):
            raise ValueError('Scanner hash differs from replay summary')
    return dedup, summary


def factor_template(events):
    result = []
    for key in sorted(events):
        e = events[key]
        result.append({'event_key':key,'symbol':e['symbol'],
                       'retest_ts':e['retest_ts'],
                       **{field:'' for field in REQUIRED_FACTOR_FIELDS if field != 'event_key'}})
    return result


def write_csv(path, rows):
    if not rows: return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open('w',newline='',encoding='utf-8') as file:
        writer=csv.DictWriter(file,fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def load_factors(path):
    rows = load_csv(path)
    if rows and not set(REQUIRED_FACTOR_FIELDS).issubset(rows[0]):
        raise ValueError(f'Faltan columnas: {sorted(set(REQUIRED_FACTOR_FIELDS)-set(rows[0]))}')
    output = {}
    for row in rows:
        key=row.get('event_key')
        if not key: raise ValueError('Factor event_key vacío')
        if key in output: raise ValueError(f'Factor duplicado: {key}')
        output[key]=row
    return output


def prepare_candidates(events, factors, *, assumed_execution_delay_ms=0):
    """Validate causal provenance and apply a historical-only execution clock.

    A 1m close known at HH:MM:00 cannot generate a fill at that exact OPEN;
    at least one millisecond must elapse. The optional extra delay is a
    scenario assumption, not a reconstruction of observed live processing.
    """
    if (isinstance(assumed_execution_delay_ms, bool)
            or not isinstance(assumed_execution_delay_ms, int)
            or assumed_execution_delay_ms < 0):
        raise ValueError('assumed_execution_delay_ms must be a nonnegative integer')
    candidates=[]; problems=[]
    for key,e in sorted(events.items(), key=lambda pair:(int(pair[1]['retest_ts']),pair[0])):
        f=factors.get(key)
        if not f:
            problems.append({'event_key':key,'reason':'MISSING_HISTORICAL_FACTORS'})
            continue
        room=as_float(f.get('room_1h_pct'))
        vol=as_float(f.get('volume_rank_pct_4h'))
        ret=as_float(f.get('return_rank_pct_4h'))
        ra=as_int(f.get('room_available_ms'))
        va=as_int(f.get('ranks_available_ms'))
        rs=as_int(f.get('room_asof_ms'))
        vs=as_int(f.get('ranks_asof_ms'))
        if None in (room,vol,ret,ra,va,rs,vs):
            problems.append({'event_key':key,'reason':'INCOMPLETE_FACTORS_OR_TIMESTAMPS'})
            continue
        if not 0 <= vol <= 100 or not 0 <= ret <= 100 or room < 0:
            problems.append({'event_key':key,'reason':'OUT_OF_RANGE_FACTORS'})
            continue
        if f.get('room_method') != 'ASOF_CONFIRMED_SWINGS_1H' or f.get('ranks_method') != 'ASOF_CLOSED_4H_CROSS_SECTION':
            problems.append({'event_key':key,'reason':'NONCAUSAL_OR_UNKNOWN_FACTOR_METHOD'})
            continue
        known=as_int(e['known_ts']); observed=as_int(e['first_replay_observed_ms'])
        if not (0 < rs <= ra and 0 < vs <= va):
            problems.append({'event_key':key,'reason':'INVALID_FACTOR_ASOF_OR_AVAILABILITY'})
            continue
        theoretical_ready=max(known,observed,ra,va)
        # A stale factor from a far-away future boundary must not be substituted.
        if theoretical_ready > known + 5*60*MINUTE:
            problems.append({'event_key':key,'reason':'CONTEXT_TOO_LATE_FOR_REACTION'})
            continue
        ready=theoretical_ready + POST_BOUNDARY_GUARD_MS + assumed_execution_delay_ms
        candidates.append({'key':key,'symbol':e['symbol'],'side':e['side'],
                           'retest_ts':as_int(e['retest_ts']),
                           'first_seen_ms':observed, # intentionally virtual, NOT live
                           'ready_ms':ready,
                           'theoretical_ready_ms':theoretical_ready,
                           'post_boundary_guard_ms':POST_BOUNDARY_GUARD_MS,
                           'assumed_execution_delay_ms':assumed_execution_delay_ms,
                           'values':(as_float(json.loads(e['causal_json'])['max_departure_pct']),room,vol,ret),
                           'context_status':'HISTORICAL_ASOF_DERIVED',
                           'regime_status':'HISTORICAL_ASOF_DERIVED',
                           'capture_method':'HISTORICAL_RECOMPUTED_ASOF_NOT_LIVE',
                           'replay_scanner_hash':e.get('source_hash','')})
    return candidates, problems


def load_candles_from_cache(cache_dir, symbols, needed_timestamps=None):
    """Read existing replay Binance-native JSON caches. No HTTP requests.

    Cache keys are hashed, so enumerate metadata and include only requested symbols.
    Gaps and conflicting duplicate 1m bars are fatal for that symbol.
    """
    by_symbol={symbol:{} for symbol in symbols}
    file_counts=Counter()
    cache_dir=Path(cache_dir)
    if not cache_dir.is_dir():
        raise FileNotFoundError(f'Candle cache missing: {cache_dir}')
    for file in cache_dir.rglob('*.json'):
        try:
            raw=json.loads(file.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            continue
        symbol=raw.get('symbol')
        if symbol not in by_symbol or raw.get('timeframe') != '1m':continue
        if raw.get('origin') != 'binance_futures_native_klines':
            raise ValueError(f'Unsupported candles origin: {file}')
        file_counts[symbol]+=1
        keep = None if needed_timestamps is None else needed_timestamps.get(symbol, set())
        for candle in raw.get('candles',()):
            ts=as_int(candle.get('timestamp'))
            if keep is not None and ts not in keep:
                continue
            vals=tuple(as_float(candle.get(k)) for k in ('open','high','low','close'))
            if ts is None or ts % MINUTE or any(v is None or v <= 0 for v in vals):
                raise ValueError(f'Invalid cached bar in {file}')
            op,hi,lo,cl=vals
            if not lo <= min(op,cl) <= max(op,cl) <= hi:
                raise ValueError(f'Invalid OHLC in {file} {ts}')
            item=(ts,*vals)
            previous=by_symbol[symbol].get(ts)
            if previous is not None and previous != item:
                raise ValueError(f'Conflicting 1m candle {symbol} {ts}')
            by_symbol[symbol][ts]=item
    return by_symbol, dict(file_counts)


def attach_paths(candidates, cache, length=240):
    """Anchor exactly at retest+1m, preserve 1m contiguity; never skip missing bar."""
    if length < 210: raise ValueError('240m recommended to observe 180m + 30m TTL')
    output=[]; problems=[]
    for r in candidates:
        start=r['retest_ts']+MINUTE
        candles=cache.get(r['symbol'],{})
        bars=[candles.get(start+i*MINUTE) for i in range(length)]
        if any(b is None for b in bars):
            missing=sum(b is None for b in bars)
            problems.append({'event_key':r['key'],'reason':'MISSING_CONTIGUOUS_1M_PATH',
                             'missing_bars':missing})
            continue
        if r['ready_ms'] >= bars[-1][0]+MINUTE:
            problems.append({'event_key':r['key'],'reason':'READY_AFTER_PATH'})
            continue
        # Exactly like original V2: reference is first post-retest native 1m OPEN.
        output.append({**r, 'bars':tuple(bars), 'reference':float(bars[0][1])})
    return output, problems


def extrema_report(comparison):
    """Post-trade diagnostic. No parameters are changed with these future metrics."""
    out=[]
    trace_index={}
    for name, res in comparison.items():
        for trace in res['path_traces']:
            if trace['stage'] not in ('IN_POSITION','EXIT_BAR'):continue
            trace_index.setdefault((name,trace['event_key']),[]).append(trace)
        for t in res['trades']:
            rows=sorted(trace_index.get((name,t['event_key']),()),key=lambda x:x['timestamp_ms'])
            if not rows:continue
            ts0=int(t['fill_ts']); ts1=as_int(t.get('exit_ts'))
            if ts1 is None:continue
            # MFE of a TP/SL exit bar may extend beyond the first touched level.
            # Clamp to take-profit on TP exits. Bar sequencing is unknown.
            tp = t.get('status')=='TP'
            sl = t.get('status') in ('SL','AMBIGUOUS_BOTH_AS_SL')
            side=t['side']; entry=float(t['fill_price']); target=as_float(rows[0].get('tp_price'))
            stop=as_float(rows[0].get('sl_price'))
            direction=1 if side=='LONG' else -1
            mfe=mae=0.; peak=valley=None; positive_closes=0; observed=0
            windows=(15,30,60,120,180)
            at={f'mfe_{n}m_pct':None for n in windows}
            at.update({f'mae_{n}m_pct':None for n in windows})
            for index,b in enumerate(rows):
                ts=int(b['timestamp_ms']); stage=b['stage']
                hi=float(b['high']);lo=float(b['low']);close=float(b['close'])
                fav=100*(hi/entry-1) if side=='LONG' else 100*(1-lo/entry)
                adv=100*(1-lo/entry) if side=='LONG' else 100*(hi/entry-1)
                if ts==ts1:
                    if tp and target is not None: fav=min(fav,abs(100*(target/entry-1)))
                    if sl and stop is not None: adv=min(adv,abs(100*(stop/entry-1)))
                if fav>mfe: mfe=fav;peak=ts
                if adv>mae: mae=adv;valley=ts
                positive_closes += int((close/entry-1)*direction>0)
                observed+=1
                for n in windows:
                    if index+1==n:
                        at[f'mfe_{n}m_pct']=mfe
                        at[f'mae_{n}m_pct']=mae
            gross=as_float(t.get('gross_pct'))
            out.append({'strategy':name,'event_key':t['event_key'],'symbol':t['symbol'],
                        'side':side,'status':t['status'],'fill_ts':ts0,'exit_ts':ts1,
                        'mfe_pct':mfe,'mae_pct':mae,'time_to_mfe_min':
                        ((peak-ts0)//MINUTE) if peak is not None else None,
                        'time_to_mae_min':((valley-ts0)//MINUTE) if valley is not None else None,
                        'gross_exit_pct':gross,'giveback_pct':
                        max(0,mfe-gross) if gross is not None else None,
                        'positive_close_minutes':positive_closes,
                        'observed_minutes':observed,
                        'bar_exit_order_unknown':True,**at})
    return out


def source_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):digest.update(block)
    return digest.hexdigest()
