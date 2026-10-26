#!/usr/bin/env python3
"""Horizontal Level Lifecycle V2.0 (phase A) — offline causal 30m atlas.

Registers a de-duplicated causal atlas from 2 *confirmed* 2x2 pivots.
Observes subsequent interactions, crossings, and returns in either direction.
No 1m outcome, PnL, trading, exchange or Redis. Reads only local candle archives.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from horizontal_breakout_lab import Archive, ROOT, TF, aggregate_15m_to_30m, normalize

VERSION = 'horizontal-level-lifecycle-v2.1-geometry'
DEFAULT_OUTPUT = Path('reports/horizontal_level_lifecycle_v21')
FORMATION_COLS = ('level_id','symbol','origin','formation_closed_ts','formation_utc',
                  'first_pivot_open_ts','second_pivot_open_ts','center','zone_low',
                  'zone_high','atr_at_formation','width_atr','pivot_separation_bars',
                  'confirmation_count','quality_flag','segment','source_30m')
EVENT_COLS = ('event_id','level_id','symbol','origin','event_type','observed_closed_ts',
              'observed_utc','bar_open_ts','previous_relation','close_relation','side_at_pre',
              'close','high','low','center','zone_low','zone_high','atr_at_formation',
              'age_bars','visit_number','test_id','test_from','test_result','reconfirmation_number','segment','source_30m')
END_COLS = ('level_id','symbol','origin','formation_closed_ts','end_state',
            'end_closed_ts','n_visits_after_formation','n_cross_up','n_cross_down',
            'n_returns','n_reconfirmations','n_tests','n_defended','n_lost','n_timed_out','bars_observed')
COVERAGE_COLS=('symbol','source_30m','bars30','segments','gaps30','levels','visits',
               'cross_up','cross_down','returns','reconfirmations','defenses','losses','timed_out','suppressed_overlaps','wide_zones','status','error')


@dataclass(frozen=True)
class Config:
    left: int = 2
    right: int = 2
    min_separation_bars: int = 5
    max_pivot_span_bars: int = 96
    min_history_bars: int = 32
    atr_bars: int = 14
    tolerance_atr: float = 0.35
    tolerance_floor_pct: float = 0.10
    min_zone_width_pct: float = 0.10
    max_age_bars: int = 160
    minimum_departure_bars: int = 2
    minimum_departure_atr: float = 0.50
    defense_window_bars: int = 4
    defense_departure_atr: float = 0.50
    overlap_threshold: float = 0.50
    wide_zone_pct: float = 2.0  # quality flag only; never remove retrospectively
    max_active_per_origin: int = 35


def _iso(ms):
    return datetime.fromtimestamp(int(ms)/1000,timezone.utc).isoformat()


def _hash(*parts):
    return hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:24]


def _relation(price, lo, hi):
    return 'ABOVE' if price > hi else ('BELOW' if price < lo else 'INSIDE')


def _atr(frame, i, n=14):
    if i < n:
        return None
    highs=frame.high.iloc[i-n+1:i+1].to_numpy(dtype=float)
    lows=frame.low.iloc[i-n+1:i+1].to_numpy(dtype=float)
    prev_closes=frame.close.iloc[i-n:i].to_numpy(dtype=float)
    tr=np.maximum(highs-lows,np.maximum(np.abs(highs-prev_closes),np.abs(lows-prev_closes)))
    val=float(np.mean(tr))
    return val if math.isfinite(val) and val>0 else None


def _pivots_confirmed_now(frame, i, cfg):
    """Pivot j=i-right is known only at close of bar i, NOT earlier."""
    j=i-cfg.right
    if j<cfg.left or i>=len(frame):
        return []
    high=float(frame.high.iloc[j]);low=float(frame.low.iloc[j])
    others_hi=np.r_[frame.high.iloc[j-cfg.left:j].to_numpy(),frame.high.iloc[j+1:i+1].to_numpy()]
    others_lo=np.r_[frame.low.iloc[j-cfg.left:j].to_numpy(),frame.low.iloc[j+1:i+1].to_numpy()]
    out=[]
    if high>max(others_hi):out.append(('HIGH',high,j))
    if low<min(others_lo):out.append(('LOW',low,j))
    return out


def _segment_bounds(frame):
    """Never join history, pivots or interactions across a missing 30m candle."""
    if frame.empty:return []
    cuts=[0]
    for i in range(1,len(frame)):
        if int(frame.timestamp.iloc[i])-int(frame.timestamp.iloc[i-1])!=TF:
            cuts.append(i)
    return [(a,b) for a,b in zip(cuts,cuts[1:]+[len(frame)])]


def _overlap_ratio(a_lo, a_hi, b_lo, b_hi):
    """Intersection divided by the *smaller* band's width, no future inputs."""
    overlap = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    width = min(a_hi-a_lo, b_hi-b_lo)
    return overlap / width if width > 0 else 0.0


def scan_symbol(symbol, thirty, cfg=Config(), source='UNKNOWN'):
    """Causal, closed-30m lifecycle; no live source or forward outcome access.

    Level centers and boundaries never repaint. Candidate zone reconfirmations
    suppress a concurrent duplicate instead of creating an extra independent level.
    A contact requires a meaningful previous departure, not merely wick chatter.
    Its *later* resolution is stored as a separate event timestamped at confirmation.
    """
    frame = normalize(thirty, TF)
    formation, events, ends = [], [], []
    suppressed = 0
    wide_zones = 0
    bounds = _segment_bounds(frame)
    for segno, (start, stop) in enumerate(bounds, 1):
        seg = frame.iloc[start:stop].reset_index(drop=True)
        active = []
        prior = {'HIGH': [], 'LOW': []}
        for i in range(len(seg)):
            bar = seg.iloc[i]
            clock = int(bar.timestamp) + TF
            if i >= cfg.min_history_bars:
                for lv in list(active):
                    age = i-lv['form_index']
                    if age > cfg.max_age_bars:
                        lv['end_state'] = 'MAX_AGE'
                        lv['end_closed_ts'] = int(seg.timestamp.iloc[i-1]) + TF
                        active.remove(lv)
                        ends.append(_end(lv))
                        continue
                    lo, hi = lv['zone_low'], lv['zone_high']
                    atr = lv['atr_at_formation']
                    pre = float(seg.close.iloc[i-1])
                    close = float(bar.close)
                    before = _relation(pre, lo, hi)
                    after = _relation(close, lo, hi)
                    intersects = float(bar.low) <= hi and float(bar.high) >= lo
                    lv['bars_observed'] += 1

                    resolved_this_bar = False
                    # A previously registered TEST can resolve only when a later
                    # 30m bar CLOSES. It cannot retroactively change that TEST.
                    pending = lv['pending_test']
                    if pending is not None and i > pending['index']:
                        direction = pending['side']
                        if direction == 'ABOVE':
                            lost = close < lo
                            defended = close > hi + cfg.defense_departure_atr*atr
                        else:
                            lost = close > hi
                            defended = close < lo - cfg.defense_departure_atr*atr
                        result = ('LOSS_CONFIRMED' if lost else 'DEFENSE_CONFIRMED'
                                  if defended else 'TEST_TIMEOUT' if
                                  i-pending['index'] >= cfg.defense_window_bars else None)
                        if result:
                            _event(events, lv, result, bar, clock, before, after,
                                   segno, source, test_id=pending['id'],
                                   test_from=direction, test_result=result)
                            lv[{'LOSS_CONFIRMED':'n_lost',
                                'DEFENSE_CONFIRMED':'n_defended',
                                'TEST_TIMEOUT':'n_timed_out'}[result]] += 1
                            lv['pending_test'] = None
                            resolved_this_bar = True

                    # CROSS and RETURN are descriptive closure transitions, NOT
                    # claims that a breakout has been accepted or made money.
                    typ = ('CROSS_UP' if after == 'ABOVE' and before != 'ABOVE'
                           else 'CROSS_DOWN' if after == 'BELOW' and before != 'BELOW'
                           else 'RETURN_INSIDE' if after == 'INSIDE' and before != 'INSIDE'
                           else None)
                    if typ:
                        _event(events, lv, typ, bar, clock, before, after, segno, source)
                        lv[{'CROSS_UP':'n_up','CROSS_DOWN':'n_down',
                            'RETURN_INSIDE':'n_return'}[typ]] += 1

                    # Only a test armed by two FULLY DISJOINT closed candles on
                    # the same side, away by >= 0.5 formation ATR, is counted.
                    # This avoids equating repeated wick chatter with defenses.
                    if (lv['armed'] and intersects and
                            lv['pending_test'] is None and not resolved_this_bar):
                        side = lv['armed_side']
                        lv['n_tests'] += 1
                        test_id = _hash(lv['level_id'], clock, 'TEST', lv['n_tests'])
                        lv['pending_test'] = {'index':i, 'id':test_id, 'side':side}
                        _event(events, lv, 'TEST_FROM_'+side, bar, clock, before,
                               after, segno, source, test_id=test_id, test_from=side)
                        lv['armed'] = False
                        lv['armed_side'] = None
                        lv['away_count'] = 0
                        lv['away_side'] = None
                    elif not intersects:
                        if (float(bar.low) > hi and close >=
                            hi+cfg.minimum_departure_atr*atr):
                            side = 'ABOVE'
                        elif (float(bar.high) < lo and close <=
                              lo-cfg.minimum_departure_atr*atr):
                            side = 'BELOW'
                        else:
                            side = None
                        if side:
                            lv['away_count'] = (lv['away_count']+1 if
                                                side==lv['away_side'] else 1)
                            lv['away_side'] = side
                            if lv['away_count'] >= cfg.minimum_departure_bars:
                                lv['armed'] = True
                                lv['armed_side'] = side
                        elif not lv['armed']:
                            lv['away_count'] = 0
                            lv['away_side'] = None
                    elif not lv['armed']:
                        lv['away_count'] = 0
                        lv['away_side'] = None
                    lv['last_relation'] = after

            if i < max(cfg.min_history_bars, cfg.atr_bars+1):
                continue
            atr = _atr(seg, i, cfg.atr_bars)
            if atr is None:
                continue
            for origin, price, j in _pivots_confirmed_now(seg, i, cfg):
                pivots = prior[origin]
                pivots[:] = [p for p in pivots
                             if j-p['j'] <= cfg.max_pivot_span_bars]
                matches = []
                for p in pivots:
                    if j-p['j'] < cfg.min_separation_bars:
                        continue
                    tolerance = max(cfg.tolerance_atr*atr,
                                    price*cfg.tolerance_floor_pct/100)
                    if abs(p['price']-price) <= tolerance:
                        matches.append((abs(p['price']-price), p))
                if matches:
                    p = min(matches, key=lambda x:(x[0],x[1]['j']))[1]
                    center = (price+p['price'])/2
                    halfwidth = max(cfg.min_zone_width_pct*center/100,
                                    cfg.tolerance_atr*atr,
                                    abs(p['price']-price)/2)
                    zone_low, zone_high = center-halfwidth, center+halfwidth
                    # Causal suppression by price overlap of same-origin ACTIVE
                    # frozen zones. Previous centers/bands are never moved.
                    overlaps = [( _overlap_ratio(zone_low,zone_high,
                                                  z['zone_low'],z['zone_high']), z)
                                for z in active if z['origin']==origin]
                    overlaps = [t for t in overlaps
                                if t[0] >= cfg.overlap_threshold]
                    if overlaps:
                        _, winner = max(overlaps, key=lambda x:(x[0],
                                                              -x[1]['formation_closed_ts']))
                        suppressed += 1
                        winner['n_reconfirmations'] += 1
                        _event(events,winner,'LEVEL_RECONFIRMED',bar,clock,
                               winner['last_relation'],
                               _relation(float(bar.close),winner['zone_low'],
                                         winner['zone_high']),segno,source,
                               reconfirmation_number=winner['n_reconfirmations'])
                    elif sum(z['origin']==origin for z in active) < cfg.max_active_per_origin:
                        level_id = _hash(VERSION,symbol,origin,
                                         int(seg.timestamp.iloc[p['j']]),
                                         int(seg.timestamp.iloc[j]))
                        pct = 200*halfwidth/center*100
                        quality = 'WIDE_ZONE_REVIEW' if pct>cfg.wide_zone_pct else 'STANDARD_WIDTH'
                        wide_zones += int(quality=='WIDE_ZONE_REVIEW')
                        lv = dict(level_id=level_id,symbol=symbol,origin=origin,
                            formation_closed_ts=clock,formation_utc=_iso(clock),
                            first_pivot_open_ts=int(seg.timestamp.iloc[p['j']]),
                            second_pivot_open_ts=int(seg.timestamp.iloc[j]),
                            center=center,zone_low=zone_low,zone_high=zone_high,
                            atr_at_formation=atr,width_atr=halfwidth/atr,
                            pivot_separation_bars=j-p['j'],confirmation_count=2,
                            quality_flag=quality,segment=segno,source_30m=source,
                            form_index=i,n_up=0,n_down=0,n_return=0,
                            n_reconfirmations=0,n_tests=0,n_defended=0,n_lost=0,
                            n_timed_out=0,bars_observed=0,away_count=0,
                            away_side=None,armed=False,armed_side=None,
                            last_relation=_relation(float(bar.close),zone_low,zone_high),
                            pending_test=None,end_state='ACTIVE_AT_END',end_closed_ts=None)
                        active.append(lv)
                        formation.append({k:lv[k] for k in FORMATION_COLS})
                pivots.append(dict(j=j, price=price))
        for lv in active:
            lv['end_state'] = 'GAP_CENSORED' if stop<len(frame) else 'ACTIVE_AT_END'
            lv['end_closed_ts'] = int(seg.timestamp.iloc[-1])+TF
            ends.append(_end(lv))
    report = dict(symbol=symbol,source_30m=source,bars30=len(frame),
        segments=len(bounds),gaps30=max(0,len(bounds)-1),levels=len(formation),
        visits=sum(e['event_type'].startswith('TEST_FROM_') for e in events),
        cross_up=sum(e['event_type']=='CROSS_UP' for e in events),
        cross_down=sum(e['event_type']=='CROSS_DOWN' for e in events),
        returns=sum(e['event_type']=='RETURN_INSIDE' for e in events),
        reconfirmations=sum(e['event_type']=='LEVEL_RECONFIRMED' for e in events),
        defenses=sum(e['event_type']=='DEFENSE_CONFIRMED' for e in events),
        losses=sum(e['event_type']=='LOSS_CONFIRMED' for e in events),
        timed_out=sum(e['event_type']=='TEST_TIMEOUT' for e in events),
        suppressed_overlaps=suppressed,wide_zones=wide_zones,
        status='COMPLETE' if len(frame)>=cfg.min_history_bars else 'INSUFFICIENT_30M',
        error='')
    return formation,events,ends,report


def _event(events, lv, kind, bar, closed, a, b, seg, source,
           test_id='', test_from='', test_result='', reconfirmation_number=0):
    events.append(dict(event_id=_hash(lv['level_id'],int(closed),kind,test_id),
        level_id=lv['level_id'],symbol=lv['symbol'],origin=lv['origin'],
        event_type=kind,observed_closed_ts=int(closed),observed_utc=_iso(closed),
        bar_open_ts=int(bar.timestamp),previous_relation=a,close_relation=b,
        side_at_pre=a,close=float(bar.close),high=float(bar.high),
        low=float(bar.low),center=lv['center'],zone_low=lv['zone_low'],
        zone_high=lv['zone_high'],atr_at_formation=lv['atr_at_formation'],
        age_bars=int((int(closed)-lv['formation_closed_ts'])//TF),
        visit_number=lv['n_tests'],test_id=test_id,test_from=test_from,
        test_result=test_result,reconfirmation_number=reconfirmation_number,
        segment=seg,source_30m=source))


def _end(lv):
    return dict(level_id=lv['level_id'],symbol=lv['symbol'],origin=lv['origin'],
        formation_closed_ts=lv['formation_closed_ts'],end_state=lv['end_state'],
        end_closed_ts=lv['end_closed_ts'],n_visits_after_formation=lv['n_tests'],
        n_cross_up=lv['n_up'],n_cross_down=lv['n_down'],
        n_returns=lv['n_return'],n_reconfirmations=lv['n_reconfirmations'],
        n_tests=lv['n_tests'],n_defended=lv['n_defended'],
        n_lost=lv['n_lost'],n_timed_out=lv['n_timed_out'],
        bars_observed=lv['bars_observed'])


def _atomic_csv(path,rows,columns):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllv2-',dir=path.parent,suffix='.csv')
    try:
        with os.fdopen(fd,'w',newline='',encoding='utf-8') as h:
            writer=csv.DictWriter(h,fieldnames=columns,extrasaction='ignore')
            writer.writeheader();writer.writerows(rows)
            h.flush();os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.hllv2-',dir=path.parent,suffix='.json')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as h:
            json.dump(value,h,indent=2,sort_keys=True,ensure_ascii=False)
            h.flush();os.fsync(h.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load_30m(arc,symbol):
    native,src=arc._bars(symbol,'30m')
    fifteen,_=arc._bars(symbol,'15m')
    if not fifteen.empty:
        derived=aggregate_15m_to_30m(fifteen)
        if native.empty:
            return derived,'PROXY_15M_PAIR'
        missing=derived.loc[~derived.timestamp.isin(native.timestamp)]
        if not missing.empty:
            # Never silently accept conflicting 30m and 15m OHLC; preference is native.
            native=normalize(pd.concat([native,missing],ignore_index=True),TF)
            src+='+PROXY_15M_PAIR'
    return native,src


def parser(argv=None):
    p=argparse.ArgumentParser(description='Causal historical support/resistance lifecycle; no LIVE or order execution')
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true');mode.add_argument('--run',action='store_true')
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--candles-dir',type=Path,default=None)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--symbols',type=str,default='')
    p.add_argument('--run-tag',type=str,default='pilot')
    p.add_argument('--min-free-gb',type=float,default=0.5)
    return p.parse_args(argv)


def main(argv=None):
    args=parser(argv)
    cfg=Config()
    if not args.run_tag or not all(x.isalnum() or x in '._-' for x in args.run_tag):
        raise SystemExit('Invalid --run-tag')
    root=args.root.resolve()
    output=root/DEFAULT_OUTPUT/args.run_tag
    with Archive(root,args.candles_dir) as arc:
        syms=arc.symbols()
        if args.symbols:
            requested=set(s.strip().upper() for s in args.symbols.split(',') if s.strip())
            syms=[s for s in syms if s in requested]
            if set(syms)!=requested:
                raise SystemExit('Missing archived symbols: '+','.join(sorted(requested-set(syms))))
        if args.max_symbols>0:syms=syms[:args.max_symbols]
        info=dict(version=VERSION,mode='PLAN_READ_ONLY' if args.plan else 'RUN_ARCHIVE_ONLY',
                  output=str(output),symbols=len(syms),config=asdict(cfg),
                  note='Zero future outcome access; no orders, no Redis, no network')
        print('HLL_V2_PLAN',json.dumps(info,ensure_ascii=False),flush=True)
        if args.plan:
            for sym in syms[:5]:
                b,src=load_30m(arc,sym)
                print('HLL_V2_COVERAGE',json.dumps(dict(symbol=sym,bars30=len(b),source=src)),flush=True)
            return 0
        if not syms:raise SystemExit('No archived symbols')
        if shutil.disk_usage(root).free/1024**3 < args.min_free_gb:
            raise SystemExit('Not enough disk space')
        fingerprint=_hash(VERSION,json.dumps(info['config'],sort_keys=True),','.join(syms),str(args.candles_dir))
        manifest=dict(version=VERSION,config=asdict(cfg),symbols=syms,source_type='CANDLES_DIR' if args.candles_dir else 'ARCHIVE_SQLITE',
                      candles_dir=str(args.candles_dir.resolve()) if args.candles_dir else None,
                      fingerprint=fingerprint)
        mfile=output/'run_config.json'
        if output.exists():
            raise SystemExit('RUN_TAG_EXISTS: this lab never overwrites a prior freeze; choose a new --run-tag')
        _atomic_json(mfile,manifest)
        shard=output/'shards';shard.mkdir(exist_ok=True)
        coverage=[]
        for ix,sym in enumerate(syms,1):
            try:
                b,src=load_30m(arc,sym)
                lv,ev,en,diag=scan_symbol(sym,b,cfg,src)
                for filename,records,cols in (('formation',lv,FORMATION_COLS),('events',ev,EVENT_COLS),('ends',en,END_COLS)):
                    _atomic_csv(shard/(sym+'.'+filename+'.csv'),records,cols)
            except Exception as exc:
                diag=dict(symbol=sym,source_30m='ERROR',bars30=0,segments=0,gaps30=0,
                    levels=0,visits=0,cross_up=0,cross_down=0,returns=0,reconfirmations=0,defenses=0,losses=0,timed_out=0,suppressed_overlaps=0,wide_zones=0,status='SOURCE_ERROR',error=f'{type(exc).__name__}: {exc}')
            coverage.append(diag)
            print(f'HLL_V2_PROGRESS {ix}/{len(syms)} {sym}: {diag["status"]} levels={diag["levels"]} visits={diag["visits"]} crossings={diag["cross_up"]+diag["cross_down"]}',flush=True)
        _atomic_csv(output/'coverage.csv',coverage,COVERAGE_COLS)
        errors=[x for x in coverage if x['status']=='SOURCE_ERROR']
        audit=dict(version=VERSION,symbols=len(syms),source_errors=len(errors),
                   bars30=sum(x['bars30'] for x in coverage),
                   levels=sum(x['levels'] for x in coverage),
                   visits=sum(x['visits'] for x in coverage),
                   cross_up=sum(x['cross_up'] for x in coverage),
                   cross_down=sum(x['cross_down'] for x in coverage),
                   returns=sum(x['returns'] for x in coverage),
                   reconfirmations=sum(x['reconfirmations'] for x in coverage),
                   defenses=sum(x['defenses'] for x in coverage),
                   losses=sum(x['losses'] for x in coverage),
                   timed_out=sum(x['timed_out'] for x in coverage),
                   suppressed_overlaps=sum(x['suppressed_overlaps'] for x in coverage),
                   wide_zones=sum(x['wide_zones'] for x in coverage),
                   source_statuses=dict(Counter(x['status'] for x in coverage)),
                   phase='CAUSAL_ATLAS_TESTS_AND_GEOMETRY_ONLY',
                   caveat='No future 1m outcomes, no TP/SL or profitability claim. Defenses are retrospectively known only at their own later 30m close.')
        _atomic_json(output/'audit.json',audit)
        print('HLL_V2_RESULT',json.dumps(audit,ensure_ascii=False),flush=True)
        return 2 if errors else 0


if __name__=='__main__':
    raise SystemExit(main())
