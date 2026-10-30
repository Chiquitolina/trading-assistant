#!/usr/bin/env python3
"""HLL Phase Reconstruction PRE — research-only, close-based swing phase audit.

Describes *all* confirmed close reversals and low-anchored recovery candidates at
0.5/1.0/1.5 ATR on 96 fully closed 30m candles before each PRE cutoff. It does
NOT classify good/bad setups, choose a best phase, generate entries, use visual
labels or POST, or modify any existing HLL/LIVE/dashboard files.

The confirmed turning point's extreme bar precedes its knowledge time: it is
known only after an ATR-sized opposite close move. The terminal leg is always
PROVISIONAL. This is a retrospective PRE snapshot, NOT a bar-by-bar backtest.
"""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import hll_structural_approach_ohlc_pre as v2
import hll_resistance_interaction_pre as v3

BAR_MS = v2.BAR_MS
LOOKBACK = 96
SCALES = (0.5, 1.0, 1.5)
V2_PATH = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_ohlc_pre_v2/attempts_overview_pre.csv')
PHASE_PATH = Path('reports/horizontal_level_second_ascent_pre/core15_two_leg_v12/segments_pre.csv')
V3_PATH = Path('reports/horizontal_level_second_ascent_pre/core15_resistance_interaction_pre_v3/resistance_windows_pre.csv')
CANDLE_DIR = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
OUTPUT_DIR = Path('reports/horizontal_level_second_ascent_pre/core15_phase_reconstruction_pre_v1')

CORE = ('attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'scale_atr')
PIVOT_FIELDS = CORE + (
    'pivot_index', 'pivot_type', 'pivot_open_ts', 'pivot_close_ts',
    'pivot_price_close', 'confirmed_known_closed_ts', 'confirmation_delay_bars',
    'confirmation_kind', 'pivot_is_window_boundary', 'status',
)
LEG_FIELDS = CORE + (
    'leg_index', 'leg_role', 'leg_direction', 'status',
    'start_pivot_type', 'start_pivot_open_ts', 'start_pivot_price_close',
    'start_pivot_known_closed_ts', 'end_extreme_open_ts', 'end_extreme_price_close',
    'end_extreme_confirmed_known_closed_ts', 'duration_to_extreme_bars',
    'bars_since_extreme', 'amplitude_atr', 'last_close_minus_extreme_atr',
    'close_path_efficiency_to_extreme', 'start_is_window_boundary',
)
CANDIDATE_FIELDS = CORE + (
    'candidate_index', 'anchor_low_open_ts', 'anchor_low_known_closed_ts',
    'anchor_is_window_boundary',
    'anchor_low_close', 'age_from_low_bars', 'age_since_confirmation_bars',
    'known_after_low_bars', 'is_latest_confirmed_low',
    'later_confirmed_lows', 'later_confirmed_highs', 'later_lowest_close_undercut',
    'net_change_from_low_atr', 'max_favorable_close_from_low_atr',
    'last_close_retention_fraction', 'max_peak_to_last_close_drawback_atr',
    'path_efficiency_since_low', 'gap_to_zone_at_low_atr',
    'gap_to_zone_at_pre_atr', 'gap_reduction_atr', 'zone_touch_episodes_since_low',
    'zone_touch_bars_since_low', 'zone_close_inside_share_since_low',
    'zone_near_0p5_atr_share_since_low', 'last_close_zone_side',
    'candidate_is_retrospective_not_trade',
)
SUMMARY_FIELDS = CORE + (
    'status', 'closed_bars', 'level_group_attempts', 'confirmed_pivots',
    'confirmed_lows', 'confirmed_highs', 'confirmed_up_legs', 'confirmed_down_legs',
    'terminal_leg_direction', 'terminal_leg_role', 'terminal_leg_provisional',
    'last_confirmed_pivot_type', 'last_confirmed_pivot_age_bars',
    'last_confirmed_low_open_ts', 'last_confirmed_low_known_closed_ts',
    'last_confirmed_low_age_bars', 'recovery_candidates',
    'last_close_zone_side', 'last_close_zone_distance_atr',
    'latest_low_net_change_atr', 'latest_low_path_efficiency',
    'latest_low_touch_episodes', 'latest_low_gap_reduction_atr',
    'latest_low_undercut', 'note',
)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _finite_positive(value, name):
    try:
        val = float(value)
    except (ValueError, TypeError) as e:
        raise ValueError(f'Invalid {name}: {value!r}') from e
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f'Nonpositive/nonfinite {name}: {value!r}')
    return val


def validate_v3_rows(rows, phase, attempts):
    """V3 must contain exactly four COMPLETE_PRE rows per V2 attempt.

    V3 writes floats rounded to 8 decimal places; compare using <= 0.5e-8
    absolute rounding tolerance, rather than requiring textual equality.
    """
    expected_windows = (12, 24, 48, 96)
    expected = {(a, n) for a in attempts for n in expected_windows}
    seen = set()
    for row in rows:
        a = row.get('attempt_id', '').strip()
        try:
            n = int(row.get('window_bars', ''))
        except (ValueError, TypeError) as e:
            raise ValueError(f'Invalid V3 window for {a}') from e
        key = (a, n)
        if key not in expected or key in seen:
            raise ValueError(f'Unexpected or duplicated V3 row {key}')
        seen.add(key)
        if row.get('status') != 'COMPLETE_PRE':
            raise ValueError(f'Incomplete V3 row {key}')
        m = phase[a]
        if row.get('symbol') != m['symbol'] or row.get('level_id','') != m['level_id']:
            raise ValueError(f'V3 symbol/level mismatch for {key}')
        if v2.parse_time(row.get('pre_closed_ts','')) != m['pre_closed_ts']:
            raise ValueError(f'V3 PRE cutoff mismatch for {key}')
        for field in ('zone_low', 'zone_high', 'atr_pre'):
            val = _finite_positive(row.get(field), field)
            # V3 export rounds to 8 decimals; tolerate only output rounding.
            if abs(val - m[field]) > 5.11e-9:
                raise ValueError(f'V3 resistance/ATR mismatch {key}: {field}')
    missing = expected - seen
    if missing:
        raise ValueError(f'Missing V3 rows ({len(missing)}): {sorted(missing)[:4]}')


def _get_96(candles_by_open, pre):
    if candles_by_open is None:
        return None
    start = pre - LOOKBACK * BAR_MS
    bars = []
    for i in range(LOOKBACK):
        ts = start + i * BAR_MS
        b = candles_by_open.get(ts)
        if b is None or b['open_ts'] != ts or b['close_ts'] != ts + BAR_MS:
            return None
        bars.append(b)
    return bars


def _pivot(bars, typ, idx, known_idx):
    if not (0 <= idx <= known_idx < len(bars)):
        raise ValueError('Pivot knowledge precedes extreme or exceeds PRE')
    return dict(type=typ, index=idx, price=bars[idx]['close'],
                open_ts=bars[idx]['open_ts'], pivot_close_ts=bars[idx]['close_ts'],
                known_closed_ts=bars[known_idx]['close_ts'],
                confirmation_delay_bars=known_idx-idx)


def close_reversal_pivots(bars, threshold):
    """Streaming close-only ZigZag with explicit confirmation timestamps.

    A pivot is confirmed only when a later CLOSED candle moved >= threshold
    in the opposite direction from the running closing extreme. OHLC highs/lows
    do not determine intra-bar order. No pivot is inferred from future candles.
    """
    if not bars:
        raise ValueError('Empty bar sequence')
    threshold = _finite_positive(threshold, 'reversal ATR threshold')
    closes = [b['close'] for b in bars]
    for x in closes:
        _finite_positive(x, 'close')
    lo = hi = 0
    mode = 0  # 0 unresolved, +1 upward leg, -1 downward leg
    extreme = 0
    pivots = []
    for i in range(1, len(bars)):
        x = closes[i]
        if mode == 0:
            if x < closes[lo]:
                lo = i
            if x > closes[hi]:
                hi = i
            if lo < hi and closes[hi] - closes[lo] >= threshold:
                pivots.append(_pivot(bars, 'LOW', lo, i))
                mode = +1
                extreme = hi
            elif hi < lo and closes[hi] - closes[lo] >= threshold:
                pivots.append(_pivot(bars, 'HIGH', hi, i))
                mode = -1
                extreme = lo
        elif mode > 0:
            if x > closes[extreme]:
                extreme = i
            elif closes[extreme] - x >= threshold:
                pivots.append(_pivot(bars, 'HIGH', extreme, i))
                mode = -1
                extreme = i
        else:
            if x < closes[extreme]:
                extreme = i
            elif x - closes[extreme] >= threshold:
                pivots.append(_pivot(bars, 'LOW', extreme, i))
                mode = +1
                extreme = i
    for j, p in enumerate(pivots):
        if j and (p['index'] <= pivots[j-1]['index'] or p['type'] == pivots[j-1]['type']):
            raise ValueError('Nonalternating / nonchronological pivots')
    return pivots, mode, extreme


def _leg(bars, m, scale, idx, start_pivot, end_idx, end_known, direction, status):
    begin = start_pivot['index']
    if end_idx < begin:
        raise ValueError('Leg ends before its anchor')
    segment = bars[begin:end_idx+1]
    start = bars[begin]['close']
    finish = bars[end_idx]['close']
    total = sum(abs(segment[k]['close'] - segment[k-1]['close']) for k in range(1,len(segment)))
    delta = finish-start
    amp = abs(delta) / m['atr_pre']
    eff = abs(delta)/total if total else 0.0
    return {
        **{k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')},
        'scale_atr':scale,'leg_index':idx,
        'leg_role':('RECOVERY_LEG' if direction == 'UP' else 'CORRECTION_LEG'),
        'leg_direction':direction,'status':status,
        'start_pivot_type':start_pivot['type'],
        'start_pivot_open_ts':bars[begin]['open_ts'],
        'start_pivot_price_close':start,
        'start_pivot_known_closed_ts':start_pivot['known_closed_ts'],
        'end_extreme_open_ts':bars[end_idx]['open_ts'],
        'end_extreme_price_close':finish,
        'end_extreme_confirmed_known_closed_ts':end_known,
        'duration_to_extreme_bars':end_idx-begin,
        'bars_since_extreme':(len(bars)-1-end_idx) if status == 'PROVISIONAL' else '',
        'amplitude_atr':amp,
        'last_close_minus_extreme_atr':((bars[-1]['close']-finish)/m['atr_pre'] if status == 'PROVISIONAL' else ''),
        'close_path_efficiency_to_extreme':eff,
        'start_is_window_boundary':int(begin == 0),
    }


def zone_side(price, zl, zh):
    if price < zl: return 'BELOW'
    if price > zh: return 'ABOVE'
    return 'INSIDE'


def gap_below(price, zl, atr):
    return max(0.0, zl - price) / atr


def candidate_from_low(bars, m, scale, p, pindex, pivots):
    idx = p['index']
    following = bars[idx:]
    atr, zl, zh = (m['atr_pre'], m['zone_low'], m['zone_high'])
    start = p['price']; last = bars[-1]['close']
    peak = max(b['close'] for b in following)
    net = (last-start)/atr
    max_gain = (peak-start)/atr
    travel = sum(abs(following[i]['close']-following[i-1]['close']) for i in range(1,len(following)))
    touches = [b['high']>=zl and b['low']<=zh for b in following]
    inside = sum(zl<=b['close']<=zh for b in following)
    near = sum(v3._range_distance(b, zl, zh)/atr <= 0.5 + 1e-12 for b in following)
    latter = pivots[pindex+1:]
    undercut = any(b['close'] < start for b in following[1:])
    return {
        **{k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')},
        'scale_atr':scale, 'candidate_index':pindex+1,
        'anchor_low_open_ts':p['open_ts'],
        'anchor_low_known_closed_ts':p['known_closed_ts'],
        'anchor_low_close':start,
        'anchor_is_window_boundary':int(idx==0),
        'age_from_low_bars':len(bars)-1-idx,
        'age_since_confirmation_bars':(m['pre_closed_ts']-p['known_closed_ts'])//BAR_MS,
        'known_after_low_bars':p['confirmation_delay_bars'],
        'is_latest_confirmed_low':int(not any(q['type']=='LOW' for q in latter)),
        'later_confirmed_lows':sum(q['type']=='LOW' for q in latter),
        'later_confirmed_highs':sum(q['type']=='HIGH' for q in latter),
        'later_lowest_close_undercut':int(undercut),
        'net_change_from_low_atr':net,
        'max_favorable_close_from_low_atr':max_gain,
        'last_close_retention_fraction':(net/max_gain if max_gain>0 else ''),
        'max_peak_to_last_close_drawback_atr':(peak-last)/atr,
        'path_efficiency_since_low':((last-start)/travel if travel else 0.0),
        'gap_to_zone_at_low_atr':gap_below(start,zl,atr),
        'gap_to_zone_at_pre_atr':gap_below(last,zl,atr),
        'gap_reduction_atr':gap_below(start,zl,atr)-gap_below(last,zl,atr),
        'zone_touch_episodes_since_low':len(v3.touch_episodes(following,zl,zh)),
        'zone_touch_bars_since_low':sum(touches),
        'zone_close_inside_share_since_low':inside/len(following),
        'zone_near_0p5_atr_share_since_low':near/len(following),
        'last_close_zone_side':zone_side(last,zl,zh),
        'candidate_is_retrospective_not_trade':1,
    }


def describe_attempt(bars, m, scale, group_size=1):
    """One PRE snapshot per scale; never uses visual labels or POST."""
    if len(bars) != LOOKBACK:
        raise ValueError('Expected 96 PRE candles')
    expected = m['pre_closed_ts'] - LOOKBACK*BAR_MS
    for i,b in enumerate(bars):
        if b['open_ts']!=expected+i*BAR_MS or b['close_ts']!=expected+(i+1)*BAR_MS:
            raise ValueError('Gap, forming or POST candle in PRE window')
    if m['pre_closed_ts'] != bars[-1]['close_ts']:
        raise ValueError('Cutoff mismatch')
    pivots, mode, extreme = close_reversal_pivots(bars, scale*m['atr_pre'])
    core = {k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')}
    pivot_rows=[{
        **core, 'scale_atr':scale,'pivot_index':i+1,
        'pivot_type':p['type'],'pivot_open_ts':p['open_ts'],
        'pivot_close_ts':p['pivot_close_ts'],'pivot_price_close':p['price'],
        'confirmed_known_closed_ts':p['known_closed_ts'],
        'confirmation_delay_bars':p['confirmation_delay_bars'],
        'confirmation_kind':'CLOSE_REVERSAL_'+str(scale)+'_ATR',
        'pivot_is_window_boundary':int(p['index']==0), 'status':'CONFIRMED_PRE',
    } for i,p in enumerate(pivots)]
    legs=[]
    for i in range(1, len(pivots)):
        left,right=pivots[i-1],pivots[i]
        direction='UP' if left['type']=='LOW' else 'DOWN'
        legs.append(_leg(bars,m,scale,len(legs)+1,left,right['index'],right['known_closed_ts'],direction,'CONFIRMED_PRE'))
    if pivots:
        left=pivots[-1]
        direction='UP' if left['type']=='LOW' else 'DOWN'
        # Last observed extreme is not a confirmed endpoint. It can be followed
        # by a small counter move insufficient to confirm a new pivot.
        legs.append(_leg(bars,m,scale,len(legs)+1,left,extreme,'',direction,'PROVISIONAL'))
    candidates=[]
    for i,p in enumerate(pivots):
        if p['type']=='LOW':
            candidates.append(candidate_from_low(bars,m,scale,p,i,pivots))
    latest_low=next((p for p in reversed(pivots) if p['type']=='LOW'),None)
    latest_c=next((c for c in reversed(candidates) if c['is_latest_confirmed_low']),None)
    last=bars[-1]['close']
    last_zone_gap=(last-m['zone_low'])/m['atr_pre'] if last < m['zone_low'] else ((last-m['zone_high'])/m['atr_pre'] if last>m['zone_high'] else 0.0)
    summary={
        **core,'scale_atr':scale,'status':'COMPLETE_PRE',
        'closed_bars':LOOKBACK, 'level_group_attempts':group_size,
        'confirmed_pivots':len(pivots),
        'confirmed_lows':sum(p['type']=='LOW' for p in pivots),
        'confirmed_highs':sum(p['type']=='HIGH' for p in pivots),
        'confirmed_up_legs':sum(l['leg_direction']=='UP' and l['status']=='CONFIRMED_PRE' for l in legs),
        'confirmed_down_legs':sum(l['leg_direction']=='DOWN' and l['status']=='CONFIRMED_PRE' for l in legs),
        'terminal_leg_direction':('UP' if mode>0 else ('DOWN' if mode<0 else 'UNRESOLVED')),
        'terminal_leg_role':('RECOVERY_LEG' if mode>0 else ('CORRECTION_LEG' if mode<0 else 'UNRESOLVED')),
        'terminal_leg_provisional':int(bool(pivots)),
        'last_confirmed_pivot_type':(pivots[-1]['type'] if pivots else ''),
        'last_confirmed_pivot_age_bars':(LOOKBACK-1-pivots[-1]['index'] if pivots else ''),
        'last_confirmed_low_open_ts':(latest_low['open_ts'] if latest_low else ''),
        'last_confirmed_low_known_closed_ts':(latest_low['known_closed_ts'] if latest_low else ''),
        'last_confirmed_low_age_bars':(LOOKBACK-1-latest_low['index'] if latest_low else ''),
        'recovery_candidates':len(candidates),
        'last_close_zone_side':zone_side(last,m['zone_low'],m['zone_high']),
        'last_close_zone_distance_atr':last_zone_gap,
        'latest_low_net_change_atr':(latest_c['net_change_from_low_atr'] if latest_c else ''),
        'latest_low_path_efficiency':(latest_c['path_efficiency_since_low'] if latest_c else ''),
        'latest_low_touch_episodes':(latest_c['zone_touch_episodes_since_low'] if latest_c else ''),
        'latest_low_gap_reduction_atr':(latest_c['gap_reduction_atr'] if latest_c else ''),
        'latest_low_undercut':(latest_c['later_lowest_close_undercut'] if latest_c else ''),
        'note':('NO_CONFIRMED_CLOSE_REVERSAL' if not pivots else 'NO_QUALITY_LABEL_OR_ENTRY'),
    }
    return summary,pivot_rows,legs,candidates


def analyze(phase, attempts, candles, scales=SCALES):
    join=v3.match_attempts(phase, attempts)
    if join['missing_phase'] or join['conflicts']:
        raise ValueError('Source mismatch: '+repr(join))
    groups=Counter((phase[a]['symbol'],phase[a]['level_id'] or (phase[a]['zone_low'], phase[a]['zone_high'])) for a in attempts)
    summaries=[];piv=[];legs=[];cand=[]
    for a,(symbol,pre) in sorted(attempts.items()):
        m=phase[a]
        bars=_get_96(candles.get(symbol),pre)
        for scale in scales:
            if bars is None:
                summaries.append({**{k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')},
                                  'scale_atr':scale,'status':'MISSING_OR_GAPPED_OHLC','closed_bars':0,
                                  'level_group_attempts':groups[(m['symbol'],m['level_id'] or (m['zone_low'],m['zone_high']))],
                                  'note':'No interpolation or future data used'})
                continue
            summary,pr,lr,cr=describe_attempt(bars,m,scale,
                groups[(m['symbol'],m['level_id'] or (m['zone_low'],m['zone_high']))])
            summaries.append(summary);piv.extend(pr);legs.extend(lr);cand.extend(cr)
    return summaries,piv,legs,cand


def write_csv(path, rows, cols):
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({k:(round(v,8) if isinstance(v,float) else v) for k,v in r.items() if k in cols})


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan',action='store_true')
    mode.add_argument('--run',action='store_true')
    p.add_argument('--phase-source',type=Path,default=PHASE_PATH)
    p.add_argument('--v2-source',type=Path,default=V2_PATH)
    p.add_argument('--v3-source',type=Path,default=V3_PATH)
    p.add_argument('--candles-dir',type=Path,default=CANDLE_DIR)
    p.add_argument('--output',type=Path,default=OUTPUT_DIR)
    p.add_argument('--timestamp-kind',choices=('open','close'),default=None)
    args=p.parse_args(argv)
    required=[args.phase_source,args.v2_source,args.v3_source]
    absent=[str(x) for x in required if not x.is_file()]
    if absent:
        p.error('Missing required CSV: '+', '.join(absent))
    try:
        phase=v3.metadata_phase(v2.read_csv(args.phase_source))
        overview=v3.metadata_v2(v2.read_csv(args.v2_source))
        join=v3.match_attempts(phase,overview)
        if join['missing_phase'] or join['conflicts'] or join['extra_phase']:
            raise ValueError('Strict PRE source join not 1:1: '+repr(join))
        v3_rows=v2.read_csv(args.v3_source)
        validate_v3_rows(v3_rows,phase,overview)
    except (ValueError,KeyError) as e:
        print('HLL_PHASE_PRE_ABORT '+str(e),flush=True)
        return 2
    symbols=sorted({s for s,_ in overview.values()})
    candles={};errors={};schemas={}
    for symbol in symbols:
        path=args.candles_dir/(symbol+'_30m.csv')
        if not path.is_file():errors[symbol]='MISSING_FILE';continue
        try:
            candles[symbol],s=v2.load_candles(path,args.timestamp_kind)
            schemas[symbol]={'rows':len(candles[symbol]),'time_col':s['time_col'],'time_kind':s['time_kind']}
        except (ValueError,KeyError) as e:
            errors[symbol]=str(e)
    plan={
        'attempts':len(overview),'symbols':len(symbols),'loaded_symbols':len(candles),
        'missing_phase':join['missing_phase'],'extra_phase_count':len(join['extra_phase']),
        'conflicts':join['conflicts'],'errors':errors,'lookback_bars':LOOKBACK,
        'scales_atr':SCALES,'v3_rows':len(v3_rows),
        'sources':{'phase':str(args.phase_source),'v2':str(args.v2_source),
                   'v3':str(args.v3_source),'candles':str(args.candles_dir)},
        'schemas':schemas,'output':str(args.output),
        'read_only_inputs':True,'no_live':True,'no_dashboard_changes':True,
        'uses_post':False,'uses_visual_labels':False,'creates_trading_signals':False,
    }
    print('HLL_PHASE_PRE_PLAN '+json.dumps(plan,ensure_ascii=False,sort_keys=True),flush=True)
    if args.plan:
        return 0 if not errors else 2
    if errors:
        print('HLL_PHASE_PRE_ABORT OHLC parsing failed; no output created',flush=True)
        return 2
    try:
        summaries,piv,legs,cand=analyze(phase,overview,candles)
    except (ValueError,KeyError) as e:
        print('HLL_PHASE_PRE_ABORT '+str(e),flush=True)
        return 2
    statuses=dict(Counter(r['status'] for r in summaries))
    # Fail closed: V3 declares all 96-candle windows complete. Any missing V4
    # window is an unexpected input mismatch and must not produce partial output.
    if statuses!={'COMPLETE_PRE':len(overview)*len(SCALES)}:
        print('HLL_PHASE_PRE_ABORT expected all V3-complete PRE windows, got '+repr(statuses),flush=True)
        return 2
    args.output.mkdir(parents=True,exist_ok=True)
    write_csv(args.output/'phase_summary_pre.csv',summaries,SUMMARY_FIELDS)
    write_csv(args.output/'phase_confirmed_pivots_pre.csv',piv,PIVOT_FIELDS)
    write_csv(args.output/'phase_legs_pre.csv',legs,LEG_FIELDS)
    write_csv(args.output/'phase_recovery_candidates_pre.csv',cand,CANDIDATE_FIELDS)
    manifest={
        'version':'HLL_PHASE_RECONSTRUCTION_PRE_V1',
        'attempts':len(overview),'scales_atr':SCALES,'lookback_closed_30m_bars':LOOKBACK,
        'status_counts':statuses,'summary_rows':len(summaries),
        'confirmed_pivot_rows':len(piv),'leg_rows':len(legs),'candidate_rows':len(cand),
        'source_sha256':{'phase':digest(args.phase_source),'v2':digest(args.v2_source),
            'v3':digest(args.v3_source),
            'candles':{s:digest(args.candles_dir/(s+'_30m.csv')) for s in symbols}},
        'source_paths':plan['sources'],'timestamp_kind':args.timestamp_kind,
        'guardrails':{'uses_post':False,'uses_visual_labels':False,
            'changes_existing_detectors':False,'creates_signals':False,
            'modifies_live':False,'modifies_dashboard':False},
        'methodology':[
            'Each attempt uses exactly its 96 fully closed 30m candles prior to PRE; gaps or future candles abort.',
            'Close-based swing reversals only: no intrabar sequence is inferred from high/low.',
            'Thresholds 0.5, 1.0, 1.5 x ATR_PRE are sensitivity resolutions, not optimized signal filters.',
            'Pivot extreme time differs from its later known/confirmed time; terminal leg is never a confirmed pivot.',
            'A pivot at the first candle is left-censored by the 96-bar window boundary and is explicitly flagged.',
            'All confirmed LOW pivots yield recovery candidates, no best chain is selected using labels or outcomes.',
            'No BASE label is claimed from OHLC alone; basing is a hypothesis for future validation.',
            'Recovery candidates are descriptive retrospective PRE views, not real-time historical entries.',
            'One symbol/level can appear multiple times; attempts and scales are not independent observations.',
            'The only manually selected variable is the prior fixed 96-bar inspection horizon; assess stability out-of-sample.',
        ],
    }
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('HLL_PHASE_PRE_DONE '+json.dumps({'attempts':len(overview),
        'summary_rows':len(summaries),'pivots':len(piv),'legs':len(legs),
        'recovery_candidates':len(cand),'statuses':statuses,'output':str(args.output)},
        ensure_ascii=False,sort_keys=True),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
