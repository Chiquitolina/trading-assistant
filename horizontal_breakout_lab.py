#!/usr/bin/env python3
"""Horizontal Breakout Lab v1 — independent, OFFLINE, causal research.

Consumes only archived/local OHLCV. Read-only SQLite; never imports LIVE execution.
PRE setup + breakout rows are intentionally separate from POST outcomes.
Missing 1m data are censored, NEVER counted as failed breakouts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
REL = Path('reports/horizontal_breakout_lab_v1')
CTX = Path('reports/reaction_lab/reaction_full_context_v1.sqlite3')
HIST = Path('reports/reaction_lab/reaction_compression_history_v1.sqlite3')
M = 60_000
TF = 30 * M
TP_PCTS = (1.0, 2.0, 3.0, 5.0)
HORIZONS = (180, 360, 1440, 2880)
VERSION = 'horizontal-breakout-lab-v1.0'


@dataclass(frozen=True)
class Config:
    windows: tuple[int, ...] = (12, 24, 36, 48)
    tolerance_atr: float = 0.50
    min_touches: int = 2
    margin_pct: float = 0.10
    ttl_bars: int = 12
    cooldown_bars: int = 4
    max_wall_distance_atr: float = 2.5
    trigger: str = '30m'  # 30m closes or observable 1m closes, not candle highs
    max_horizon_min: int = 2880
    fee_roundtrip_pct: float = 0.10
    stop_pct: float = 1.50


def utc(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def serial(obj):
    if isinstance(obj, (np.integer, np.floating)):
        return obj.item()
    if isinstance(obj, (Path, tuple)):
        return str(obj)
    raise TypeError(type(obj).__name__)


def ro(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    return con


def has_candles(con):
    return con is not None and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candles'").fetchone() is not None


def empty():
    return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume'])


def normalize(df, tf):
    if df is None or len(df) == 0:
        return empty()
    df = df.copy()
    if 'timestamp' not in df.columns:
        for alt in ('open_ts', 'open_time', 'time'):
            if alt in df.columns:
                df = df.rename(columns={alt: 'timestamp'})
                break
    if 'timestamp' not in df.columns:
        raise ValueError('timestamp/open_ts missing')
    if not pd.api.types.is_numeric_dtype(df['timestamp']):
        vals = pd.to_numeric(df['timestamp'], errors='coerce')
        if vals.isna().any():
            vals = pd.to_datetime(df['timestamp'], utc=True, errors='coerce')
            if vals.isna().any():
                raise ValueError('Invalid timestamps')
            df['timestamp'] = vals.astype('int64') // 1_000_000
        else:
            df['timestamp'] = vals
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='raise').astype('int64')
    if df['timestamp'].max() < 100_000_000_000:
        df['timestamp'] *= 1000
    if 'quoteVolume' not in df:
        df['quoteVolume'] = df['quote_volume'] if 'quote_volume' in df else np.nan
    names = ['open', 'high', 'low', 'close', 'volume']
    for name in names + ['quoteVolume']:
        df[name] = pd.to_numeric(df[name], errors='coerce')
    df = df[['timestamp'] + names + ['quoteVolume']].sort_values('timestamp')
    duplicates = df[df.duplicated('timestamp', keep=False)]
    for _, group in duplicates.groupby('timestamp'):
        if not np.allclose(group[names].to_numpy(), group[names].iloc[0].to_numpy(dtype=float), equal_nan=True, atol=1e-10):
            raise ValueError('Conflicting duplicate OHLCV')
    df = df.drop_duplicates('timestamp').reset_index(drop=True)
    v = df[names].to_numpy(dtype=float)
    if (not np.isfinite(v).all() or np.any(v[:, 0:4] <= 0) or np.any(v[:, 4] < 0)
            or np.any(v[:, 1] < np.max(v[:, [0, 2, 3]], axis=1))
            or np.any(v[:, 2] > np.min(v[:, [0, 1, 3]], axis=1))):
        raise ValueError('Invalid OHLCV values')
    if (df.timestamp % tf != 0).any():
        raise ValueError(f'Unaligned timestamps for {tf}ms candles')
    return df


def aggregate_15m_to_30m(df):
    if df.empty:
        return empty()
    vals = df.set_index('timestamp')
    out = []
    for t in vals.index:
        t = int(t)
        if t % TF or t + TF // 2 not in vals.index:
            continue
        a, b = vals.loc[t], vals.loc[t + TF // 2]
        out.append((t, float(a.open), max(a.high, b.high), min(a.low, b.low), float(b.close),
                    float(a.volume + b.volume), float(a.quoteVolume + b.quoteVolume)))
    return pd.DataFrame(out, columns=empty().columns)


class Archive:
    def __init__(self, root, candles_dir=None):
        self.root = Path(root)
        self.dir = Path(candles_dir) if candles_dir else None
        self.ctx = None
        self.hist = None

    def __enter__(self):
        if self.dir:
            if not self.dir.is_dir():
                raise FileNotFoundError(self.dir)
        else:
            self.ctx = ro(self.root / CTX) if (self.root / CTX).is_file() else None
            self.hist = ro(self.root / HIST) if (self.root / HIST).is_file() else None
            if not has_candles(self.ctx) and not has_candles(self.hist):
                raise FileNotFoundError('No usable local candle archive. Supply --candles-dir.')
        return self

    def __exit__(self, *exc):
        if self.ctx: self.ctx.close()
        if self.hist: self.hist.close()

    def symbols(self):
        if self.dir:
            found = set()
            for ext in ('csv', 'parquet'):
                for f in self.dir.glob(f'*_30m.{ext}'):
                    found.add(f.name.rsplit('_30m.', 1)[0].upper())
                for f in self.dir.glob(f'*_15m.{ext}'):
                    found.add(f.name.rsplit('_15m.', 1)[0].upper())
            return sorted(found)
        found = set()
        for con in (self.ctx, self.hist):
            if has_candles(con):
                found.update(str(x[0]).upper() for x in con.execute("SELECT DISTINCT symbol FROM candles WHERE timeframe IN ('15m','30m')"))
        return sorted(found)

    def _bars(self, symbol, tf):
        if self.dir:
            for ext in ('parquet', 'csv'):
                p = self.dir / f'{symbol}_{tf}.{ext}'
                if p.is_file():
                    return normalize(pd.read_parquet(p) if ext == 'parquet' else pd.read_csv(p), {'1m':M,'15m':TF//2,'30m':TF}[tf]), f'FILE_{ext.upper()}'
            return empty(), 'FILE_MISSING'
        merged = {}
        provenance = set()
        for label, con in (('FULL_CONTEXT', self.ctx), ('HISTORY', self.hist)):
            if not has_candles(con):
                continue
            result = con.execute(
                "SELECT open_ts,open,high,low,close,volume,quote_volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY open_ts",
                (symbol, tf))
            for row in result:
                stamp = int(row[0])
                values = tuple(float(x) if x is not None else np.nan for x in row[1:])
                if stamp in merged:
                    if not np.allclose(merged[stamp][:5], values[:5], rtol=1e-9, atol=1e-10, equal_nan=True):
                        raise ValueError(f'Conflicting archival bars for {symbol} {tf} @ {stamp}')
                else:
                    merged[stamp] = values
                    provenance.add(label)
        if not merged:
            return empty(), 'ARCHIVE_MISSING'
        data = [(t, *merged[t]) for t in sorted(merged)]
        return normalize(pd.DataFrame(data, columns=empty().columns), {'1m':M, '15m':TF//2,'30m':TF}[tf]), '+'.join(sorted(provenance))

    def load(self, symbol):
        native, source = self._bars(symbol, '30m')
        fifteen, _ = self._bars(symbol, '15m')
        if not fifteen.empty:
            derived = aggregate_15m_to_30m(fifteen)
            if native.empty:
                native, source = derived, 'PROXY_15M_PAIR'
            elif not derived.empty:
                missing = derived[~derived.timestamp.isin(native.timestamp)]
                native = normalize(pd.concat([native, missing], ignore_index=True), TF)
                if not missing.empty: source += '+PROXY_15M_PAIR'
        one, one_source = self._bars(symbol, '1m')
        return native, one, source, one_source


def atr14(frame):
    x = frame.tail(15)
    if len(x) < 15:
        return None
    h, l, c = x.high.to_numpy(), x.low.to_numpy(), x.close.to_numpy()
    tr = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    val = float(np.mean(tr))
    return val if val > 0 and math.isfinite(val) else None


def episodes(indices, min_gap=2):
    out = []
    for i in indices:
        if not out or int(i) - out[-1][-1] >= min_gap:
            out.append([int(i)])
        else:
            out[-1].append(int(i))
    return [x[len(x)//2] for x in out]


def features(frame, window, cfg):
    """PRE ONLY; formation timestamp = after last 30m bar closes."""
    if len(frame) < max(window, 15): return None
    f = frame.tail(window)
    # ATR and the structural window must both have continuous CLOSED history.
    needed = frame.tail(max(window,15))
    if not np.all(np.diff(needed.timestamp.to_numpy(dtype='int64')) == TF): return None
    atr = atr14(frame)
    if atr is None: return None
    highs = f.high.to_numpy(dtype=float)
    lows = f.low.to_numpy(dtype=float)
    closes = f.close.to_numpy(dtype=float)
    v = f.volume.to_numpy(dtype=float)
    tolerance = max(cfg.tolerance_atr * atr, 0.001 * float(closes[-1]))
    # A horizontal ceiling must be anchored to observed local highs, not an
    # arbitrary quantile of every candle's high. A pivot is confirmed only
    # after the following 30m candle has already closed (causal 1x1).
    confirmed_peaks = np.array([j for j in range(1,len(highs)-1)
                                if highs[j]>=highs[j-1] and highs[j]>highs[j+1]],dtype=int)
    if not len(confirmed_peaks): return None
    ceiling_candidates = np.unique(highs[confirmed_peaks])
    best = None
    for anchor in ceiling_candidates:
        ids = np.flatnonzero(np.abs(highs - anchor) <= tolerance)
        visits = episodes(ids)
        if len(visits) < cfg.min_touches: continue
        level = float(np.median(highs[ids]))
        distance = (level - closes[-1]) / atr
        # Reject setups already broken and bases too far from the ceiling.
        if not (-0.15 <= distance <= cfg.max_wall_distance_atr): continue
        if np.any(closes[-3:] > level * (1+cfg.margin_pct/100)): continue
        # Frozen selection: strongest repeated level with minimal dispersion.
        dispersion = float(np.std(highs[ids])) / atr
        rating = (len(visits) / math.sqrt(window)) - 0.12*dispersion - 0.02*max(distance, 0)
        rec = dict(window=window, level=level, zone_low=level-tolerance,
                   zone_high=level+tolerance, tolerance_price=tolerance,
                   touch_count=len(visits), touch_open_timestamps=json.dumps([int(f.timestamp.iloc[i]) for i in visits]),
                   atr_pre=atr, wall_distance_atr=distance,
                   base_height_pct=100*(float(highs.max())-float(lows.min()))/level,
                   top_flatness_atr=dispersion, pressure_higher_lows_pct=100*(float(np.median(lows[-max(3,window//3):])) /
                     max(float(np.median(lows[:max(3,window//3)])),1e-12)-1),
                   volume_pre_recent_ratio=(float(np.mean(v[-6:]))/float(np.mean(v[-min(24,window):]))
                                             if np.mean(v[-min(24,window):])>0 else None),
                   volume_pre_total=float(v.sum()),
                   baseline_quote_volume=float((f.quoteVolume.fillna(f.volume*f.close)).median()),
                   previous_trend_pct=None)
        if len(frame) >= window + 12:
            earlier = frame.iloc[-window-12:-window]
            if len(earlier): rec['previous_trend_pct'] = 100*(float(f.close.iloc[0])/float(earlier.close.iloc[0])-1)
        key = (rating, len(visits), -dispersion, -window, level)
        if best is None or key > best[0]: best = (key, rec)
    return best[1] if best else None


def select_setup(frame, cfg):
    choices = [features(frame, w, cfg) for w in cfg.windows]
    choices = [x for x in choices if x is not None]
    if not choices: return None
    # Snapshot-specific choice independent of future outcomes.
    return max(choices, key=lambda x:(x['touch_count']/math.sqrt(x['window'])-0.12*x['top_flatness_atr']-0.02*max(x['wall_distance_atr'],0),
                                     x['touch_count'],-x['window']))


def path_outcome(one, entry_ts, level, cfg):
    """No gaps are silently interpolated. Return outcomes for each fixed horizon."""
    answer = dict(entry_ts=entry_ts, entry_price=None, path_state='NO_FILL_1M',
                  minutes_observed=0, first_missing_ts=entry_ts, mfe_pct=None, mae_pct=None,
                  first_close_below_level_min=None, retest_level_180=None)
    for h in HORIZONS:
        for tp in TP_PCTS:
            answer[f'tp{int(tp)}_h{h}'] = 'CENSORED'
            answer[f'net_tp{int(tp)}_h{h}_pct'] = None
    if one.empty: return answer
    timestamps = one.timestamp.to_numpy(dtype='int64')
    i = int(np.searchsorted(timestamps, entry_ts))
    if i >= len(timestamps) or int(timestamps[i]) != entry_ts:
        return answer
    price = float(one.open.iloc[i])
    answer['entry_price'] = price
    for h in HORIZONS:
        answer[f'mfe_h{h}_pct'] = None
        answer[f'mae_h{h}_pct'] = None
    maximum = min(cfg.max_horizon_min, max(HORIZONS))
    available = min(maximum,len(timestamps)-i)
    actual = timestamps[i:i+available]
    expected = entry_ts + np.arange(available,dtype='int64')*M
    problems = np.flatnonzero(actual!=expected)
    count = int(problems[0]) if len(problems) else available
    first_missing = entry_ts + count*M if count<maximum else None
    selection = one.iloc[i:i+count]
    highs = selection.high.to_numpy(dtype=float)
    lows = selection.low.to_numpy(dtype=float)
    closes = selection.close.to_numpy(dtype=float)
    sl = price * (1 - cfg.stop_pct/100)
    below_indices = np.flatnonzero(closes<level)
    below = int(below_indices[0]+1) if len(below_indices) else None
    touch180 = bool(np.any(lows[:180] <= level)) if count>=180 else None
    answer.update(entry_price=price,minutes_observed=count,first_missing_ts=first_missing,
                  path_state='COMPLETE' if count>=maximum else 'GAPPED_OR_SHORT',
                  mfe_pct=100*(max(float(highs.max()),price)/price-1) if count else None,
                  mae_pct=100*(min(float(lows.min()),price)/price-1) if count else None,
                  first_close_below_level_min=below,retest_level_180=touch180)
    sl_indices = np.flatnonzero(lows<=sl)
    first_sl = int(sl_indices[0]) if len(sl_indices) else None
    for h in HORIZONS:
        if count>=h:
            answer[f'mfe_h{h}_pct'] = 100*(max(float(highs[:h].max()),price)/price-1)
            answer[f'mae_h{h}_pct'] = 100*(min(float(lows[:h].min()),price)/price-1)
        else:
            answer[f'mfe_h{h}_pct'] = None
            answer[f'mae_h{h}_pct'] = None
        for tp in TP_PCTS:
            hits = np.flatnonzero(highs>=price*(1+tp/100))
            first_tp = int(hits[0]) if len(hits) else None
            if first_tp is not None and first_tp<h and (first_sl is None or first_tp<first_sl):
                label='TP_FIRST'
                gross=tp
            elif first_sl is not None and first_sl<h and (first_tp is None or first_sl<first_tp):
                label='SL_FIRST'
                gross=-cfg.stop_pct
            elif first_tp is not None and first_tp<h and first_sl==first_tp:
                label='BOTH_SAME_1M'
                gross=None
            elif count>=h:
                label='NO_HIT'
                gross=100*(float(closes[h-1])/price-1)
            else:
                label='CENSORED'
                gross=None
            answer[f'tp{int(tp)}_h{h}'] = label
            answer[f'net_tp{int(tp)}_h{h}_pct'] = gross-cfg.fee_roundtrip_pct if gross is not None else None
    return answer


def analyze_symbol(symbol, bars30, bars1, cfg, source30='UNKNOWN', source1='UNKNOWN'):
    bars30 = normalize(bars30, TF)
    bars1 = normalize(bars1, M)
    setups, breaks, outcomes = [], [], []
    active = None
    cooldown_until = -1
    diag = Counter(source_30m=source30, source_1m=source1, bars_30m=len(bars30), bars_1m=len(bars1),
                   uncovered_30m_gaps=int(np.count_nonzero(np.diff(bars30.timestamp.to_numpy()) != TF)) if len(bars30)>1 else 0)
    stamps = bars1.timestamp.to_numpy(dtype='int64')
    for i in range(len(bars30)):
        row = bars30.iloc[i]
        close_ts = int(row.timestamp) + TF
        if i and int(row.timestamp) - int(bars30.timestamp.iloc[i-1]) != TF:
            if active:
                active['end_state'] = 'GAP_30M'
                setups.append(active)
                active = None
            diag['gaps_30m_encountered'] += 1
            cooldown_until = close_ts
        if active:
            if cfg.trigger == '30m':
                break_ts = close_ts if float(row.close) > active['level'] * (1 + cfg.margin_pct/100) else None
                trigger_close = float(row.close) if break_ts is not None else None
                trigger_volume_ratio = (float(row.volume) / active['volume_baseline_30m']
                                        if active['volume_baseline_30m'] > 0 else None)
                if float(row.close) < active['zone_low'] - active['atr_pre']:
                    active['end_state'] = 'INVALIDATED_DOWN'
            else:
                start = max(active['formation_closed_ts'], int(row.timestamp))
                # Check 1m candles one by one. A later gap CANNOT erase an
                # earlier confirmed signal (the replay must be online-causal).
                pos = int(np.searchsorted(stamps, start))
                break_ts,trigger_close,trigger_volume_ratio = None,None,None
                for stamp in range(start, close_ts, M):
                    if pos >= len(stamps) or int(stamps[pos]) != stamp:
                        active['end_state'] = 'UNOBSERVABLE_1M'
                        break
                    current = bars1.iloc[pos]
                    if float(current.close) > active['level']*(1 + cfg.margin_pct/100):
                        break_ts = stamp + M
                        trigger_close = float(current.close)
                        prior = bars1.iloc[max(0,pos-20):pos]
                        contiguous = (len(prior)==20 and np.all(np.diff(prior.timestamp.to_numpy(dtype='int64'))==M)
                                      and int(prior.timestamp.iloc[-1])==stamp-M)
                        trigger_volume_ratio = float(current.volume)/float(prior.volume.mean()) if contiguous and prior.volume.mean()>0 else None
                        break
                    pos += 1
            if break_ts is not None:
                active.update(end_state='BREAKOUT',breakout_ts=break_ts)
                setups.append(active)
                event = {**active,'breakout_close':trigger_close,'breakout_volume_ratio':trigger_volume_ratio,
                         'breakout_strength_pct':100*(trigger_close/active['level']-1),
                         'trigger':cfg.trigger}
                breaks.append(event)
                outcomes.append(dict(setup_id=active['setup_id'],symbol=symbol,breakout_ts=break_ts,
                                     **path_outcome(bars1,break_ts,active['level'],cfg)))
                active = None
                cooldown_until = close_ts + cfg.cooldown_bars*TF
            elif active.get('end_state') in ('UNOBSERVABLE_1M','INVALIDATED_DOWN'):
                setups.append(active)
                active = None
                cooldown_until = close_ts
            elif close_ts - active['formation_closed_ts'] >= cfg.ttl_bars*TF:
                active['end_state'] = 'EXPIRED_NO_BREAK'
                setups.append(active)
                active = None
                cooldown_until = close_ts + TF
        if active is not None or close_ts < cooldown_until:
            continue
        if i+1 < max(cfg.windows) + 2:
            continue
        # Crucial: a candle that was just used as a breakout is never used as
        # part of another formation on this same close.
        if setups and setups[-1].get('breakout_ts') == close_ts:
            continue
        s = select_setup(bars30.iloc[:i+1],cfg)
        if s:
            started = close_ts
            ident = hashlib.sha256(f'{symbol}|{started}|{s["level"]:.12g}|{s["window"]}'.encode()).hexdigest()[:20]
            active = dict(setup_id=ident,symbol=symbol,formation_closed_ts=started,
                          formation_time_utc=utc(started),source_30m=source30,source_1m=source1,
                          **s,volume_baseline_30m=float(bars30.iloc[max(0,i-20):i].volume.mean()) if i else 0,
                          end_state='ACTIVE_AT_END',breakout_ts=None)
    if active: setups.append(active)
    diag['setups'] = len(setups)
    diag['breakouts'] = len(breaks)
    diag['resolved_1m_complete_48h'] = sum(x['path_state']=='COMPLETE' for x in outcomes)
    return setups,breaks,outcomes,dict(diag)


def atomic_write_csv(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.csv.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            frame.to_csv(f, index=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def atomic_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=path.parent,suffix='.json.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(obj,f,ensure_ascii=False,indent=2,default=serial,allow_nan=False)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def arguments(argv=None):
    p=argparse.ArgumentParser(description='Offline research: local horizontal breakout, no LIVE actions')
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    mode.add_argument('--audit',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--candles-dir',type=Path,help='Folder with SYMBOL_30m.csv/SYMBOL_1m.csv or parquet; optionally SYMBOL_15m.csv')
    p.add_argument('--symbols',default='',help='Comma-separated symbols. Default: all indexed local symbols')
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--windows',default='12,24,36,48')
    p.add_argument('--touches',type=int,default=2)
    p.add_argument('--tolerance-atr',type=float,default=.5)
    p.add_argument('--margin-pct',type=float,default=.10)
    p.add_argument('--trigger',choices=('30m','1m'),default='30m')
    p.add_argument('--min-free-gb',type=float,default=1)
    p.add_argument('--run-tag',default='default',help='Use a new tag after source data are updated')
    return p.parse_args(argv)


def config_from_args(a):
    windows=tuple(sorted(set(int(x.strip()) for x in a.windows.split(','))))
    if not windows or min(windows)<12 or max(windows)>120:
        raise ValueError('12 <= windows <= 120')
    if not 1 <= a.touches <= 8 or not .05 <= a.tolerance_atr <= 2.0 or not 0 <= a.margin_pct <= 2.0:
        raise ValueError('Invalid structural thresholds')
    return Config(windows=windows,min_touches=a.touches,tolerance_atr=a.tolerance_atr,
                  margin_pct=a.margin_pct,trigger=a.trigger)


def source_snapshot(a):
    files = sorted(p for p in Path(a.candles_dir).glob('*') if p.is_file() and p.suffix.lower() in ('.csv','.parquet')) if a.candles_dir else [a.root / CTX,a.root / HIST]
    return {str(p.resolve()): {'bytes':p.stat().st_size,'modified_ns':p.stat().st_mtime_ns}
            for p in files if p.is_file()}


def manifest(a,cfg):
    if not a.run_tag or len(a.run_tag)>80 or not all(ch.isalnum() or ch in '._-' for ch in a.run_tag):
        raise ValueError('run-tag must be 1..80 alphanumeric/dash/underscore/dot characters')
    return dict(version=VERSION,settings=asdict(cfg),source_type='CANDLES_DIR' if a.candles_dir else 'LOCAL_SQLITE',
                root=str(a.root.resolve()),candles_dir=str(a.candles_dir.resolve()) if a.candles_dir else None,
                symbols=a.symbols.upper(),max_symbols=a.max_symbols,run_tag=a.run_tag,
                important_limit='Archived REACTION-centric candles may be sparse. COMPLETE coverage must be audited.')


def paths(a,cfg):
    m=manifest(a,cfg)
    fingerprint=hashlib.sha256(json.dumps(m,sort_keys=True).encode()).hexdigest()[:14]
    return a.root / REL / 'runs' / fingerprint, m


def load_all_frames(out):
    frames=[]
    for filename in ('setups','breakouts','outcomes'):
        folder=out/'shards'
        shards=[pd.read_csv(f) for f in sorted(folder.glob(f'*.{filename}.csv')) if f.stat().st_size and len(f.read_text(encoding='utf-8').splitlines())>1]
        frames.append(pd.concat(shards,ignore_index=True) if shards else pd.DataFrame())
    return frames


def audit_run(out):
    conf=out/'run_config.json'
    if not conf.is_file(): raise RuntimeError('Missing run_config.json — run --run first')
    planned=json.loads(conf.read_text(encoding='utf-8'))
    manifests=list((out/'shards').glob('*.status.json'))
    bad=[]; total=Counter()
    expected_path=out/'expected_symbols.json'
    expected=json.loads(expected_path.read_text(encoding='utf-8')) if expected_path.is_file() else []
    completed={json.loads(f.read_text(encoding='utf-8'))['symbol'] for f in manifests}
    if not expected_path.is_file(): bad.append('MISSING_EXPECTED_SYMBOLS_MANIFEST')
    missing=sorted(set(expected)-completed)
    if missing:bad.append(f'UNPROCESSED_SYMBOLS:{len(missing)}')
    for f in manifests:
        status=json.loads(f.read_text(encoding='utf-8'))
        for label in ('setups','breakouts','outcomes'):
            fp=out/'shards'/f'{status["symbol"]}.{label}.csv'
            if not fp.is_file() or hashlib.sha256(fp.read_bytes()).hexdigest() != status['sha256'][label]:
                bad.append(f'{status["symbol"]}:{label}')
        total.update({k:v for k,v in status['diag'].items() if isinstance(v,(int,float))})
    setups,breaks,outcomes=load_all_frames(out)
    if not setups.empty and setups.setup_id.duplicated().any(): bad.append('DUPLICATE_SETUP_ID')
    if not breaks.empty and breaks.setup_id.duplicated().any(): bad.append('DUPLICATE_BREAKOUT_ID')
    if not outcomes.empty and outcomes.setup_id.duplicated().any(): bad.append('DUPLICATE_OUTCOME_ID')
    if len(breaks)!=len(outcomes):bad.append('BREAKOUT_OUTCOME_COUNT_MISMATCH')
    if not setups.empty and not breaks.empty and not set(breaks.setup_id).issubset(set(setups.setup_id)):
        bad.append('BREAKOUT_WITHOUT_FROZEN_SETUP')
    counts={}
    if not outcomes.empty:
        counts={'path_states':outcomes.path_state.value_counts(dropna=False).to_dict(),
                'tp2_h360':outcomes.tp2_h360.value_counts(dropna=False).to_dict()}
    report=dict(success=not bad,run_manifest=planned,completed_symbols=len(manifests),
                formed_setups=len(setups),breakouts=len(breaks),outcomes=len(outcomes),
                expected_symbols=len(expected),missing_symbols=missing[:50],
                totals=dict(total),outcome_counts=counts,errors=bad[:30],
                caveat='No profitability inferred without resolving costs, overlap, censoring and OOS holdout.')
    atomic_json(out/'audit.json',report)
    return report,setups,breaks,outcomes


def main(argv=None):
    a=arguments(argv)
    cfg=config_from_args(a)
    out,mt=paths(a,cfg)
    if a.audit:
        result,*_=audit_run(out)
        print(json.dumps(result,ensure_ascii=False,default=serial,indent=2))
        return 0 if result['success'] else 2
    with Archive(a.root,a.candles_dir) as archive:
        symbols=archive.symbols()
        if a.symbols:
            wanted=set(s.strip().upper() for s in a.symbols.split(',') if s.strip())
            symbols=[s for s in symbols if s in wanted]
            if len(symbols)!=len(wanted): print('SYMBOLS_NOT_FOUND',sorted(wanted-set(symbols)),flush=True)
        if a.max_symbols>0:symbols=symbols[:a.max_symbols]
        print(json.dumps(dict(mode='PLAN' if a.plan else 'RUN',output=str(out),symbols=len(symbols),
                              settings=asdict(cfg),archive_source=mt['source_type'],
                              note='No data downloaded. Source completeness is NOT guaranteed.'),ensure_ascii=False),flush=True)
        if a.plan:
            for symbol in symbols[:min(5,len(symbols))]:
                try:
                    thirty,minute,s30,s1=archive.load(symbol)
                    def longest(frame,step):
                        if frame.empty:return 0
                        changes=np.diff(frame.timestamp.to_numpy(dtype='int64'))
                        runs=np.split(np.ones(len(frame),dtype=int),np.where(changes!=step)[0]+1)
                        return max(map(len,runs),default=0)
                    print('COVERAGE_SAMPLE',json.dumps(dict(symbol=symbol,bars30=len(thirty),
                        longest_contiguous_30m=longest(thirty,TF),bars1=len(minute),
                        longest_contiguous_1m=longest(minute,M),source_30m=s30,source_1m=s1)),flush=True)
                except Exception as exc:
                    print(f'COVERAGE_SAMPLE_ERROR {symbol}: {exc}',flush=True)
            return 0
        if shutil.disk_usage(a.root).free/1024**3 < a.min_free_gb:
            raise RuntimeError('Insufficient free disk; nothing was modified')
        out.mkdir(parents=True,exist_ok=True)
        if (out/'run_config.json').exists() and json.loads((out/'run_config.json').read_text())!=mt:
            raise RuntimeError('Run config mismatch')
        snapshot=source_snapshot(a)
        stamp_path=out/'source_snapshot.json'
        if stamp_path.is_file() and json.loads(stamp_path.read_text(encoding='utf-8'))!=snapshot:
            raise RuntimeError('Archive content metadata changed since this run. Use a NEW --run-tag to prevent mixed historical data.')
        atomic_json(stamp_path,snapshot)
        expected_path=out/'expected_symbols.json'
        if expected_path.is_file() and json.loads(expected_path.read_text(encoding='utf-8'))!=symbols:
            raise RuntimeError('Symbol universe changed; use a new --run-tag.')
        atomic_json(expected_path,symbols)
        atomic_json(out/'run_config.json',mt)
        (out/'shards').mkdir(exist_ok=True)
        for ix,symbol in enumerate(symbols,1):
            status_file=out/'shards'/f'{symbol}.status.json'
            if status_file.exists():
                status=json.loads(status_file.read_text())
                if all((out/'shards'/f'{symbol}.{k}.csv').exists() and hashlib.sha256(
                        (out/'shards'/f'{symbol}.{k}.csv').read_bytes()).hexdigest()==status['sha256'].get(k)
                       for k in ('setups','breakouts','outcomes')):
                    print(f'[{ix}/{len(symbols)}] RESUME {symbol}',flush=True)
                    continue
            try:
                thirty,minute,src30,src1=archive.load(symbol)
                su,br,oc,diag=analyze_symbol(symbol,thirty,minute,cfg,src30,src1)
                files={}
                for kind,rows in [('setups',su),('breakouts',br),('outcomes',oc)]:
                    fp=out/'shards'/f'{symbol}.{kind}.csv'
                    atomic_write_csv(fp,rows)
                    files[kind]=hashlib.sha256(fp.read_bytes()).hexdigest()
                atomic_json(status_file,dict(symbol=symbol,sha256=files,diag=diag))
                print(f'[{ix}/{len(symbols)}] OK {symbol}: setups={len(su)} breakout={len(br)} complete48h={diag["resolved_1m_complete_48h"]}',flush=True)
            except Exception as exc:
                print(f'[{ix}/{len(symbols)}] ERROR {symbol}: {type(exc).__name__}: {exc}',flush=True)
                # Do not mislabel failed symbols as no-pattern. Audit sees missing shard.
        report,setups,breaks,outcomes=audit_run(out)
        atomic_write_csv(out/'setups.csv',setups.to_dict('records'))
        atomic_write_csv(out/'breakouts.csv',breaks.to_dict('records'))
        atomic_write_csv(out/'outcomes.csv',outcomes.to_dict('records'))
        print('HORIZONTAL_BREAKOUT_LAB_DONE',json.dumps({k:report[k] for k in ('success','formed_setups','breakouts','completed_symbols','outcome_counts')},ensure_ascii=False),flush=True)
        return 0 if report['success'] and len(symbols)==report['completed_symbols'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
