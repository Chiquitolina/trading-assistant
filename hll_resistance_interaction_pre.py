#!/usr/bin/env python3
"""HLL Resistance Interaction PRE V3 — descriptive, read-only, causal.

Reconstruct interaction of CLOSED 30m candles with each attempt's original
[zone_low, zone_high] resistance. All attempts and windows are retained.
No trading signals, POST, dashboard edits, label-based selection or LIVE writes.

Requires hll_structural_approach_ohlc_pre.py (V2) in the same project root.
"""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import hll_structural_approach_ohlc_pre as v2

BAR_MS = v2.BAR_MS
WINDOWS = (12, 24, 48, 96)
DEFAULT_PHASE = Path('reports/horizontal_level_second_ascent_pre/core15_two_leg_v12/phase_flags_pre.csv')
DEFAULT_V2 = Path('reports/horizontal_level_second_ascent_pre/core15_structural_approach_ohlc_pre_v2/attempts_overview_pre.csv')
DEFAULT_CANDLES = Path('reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc')
DEFAULT_OUTPUT = Path('reports/horizontal_level_second_ascent_pre/core15_resistance_interaction_pre_v3')

COLUMNS = [
    'attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'window_bars', 'status',
    'zone_low', 'zone_high', 'zone_mid', 'atr_pre',
    'first_candle_open_ts', 'last_candle_close_ts',
    'first_close_relative_zone_atr', 'last_close_relative_zone_atr',
    'start_below_zone_gap_atr', 'end_below_zone_gap_atr', 'below_gap_reduction_atr',
    'touch_bars', 'touch_bars_share', 'touch_episodes', 'close_inside_zone_bars',
    'close_inside_zone_share', 'close_above_zone_share', 'close_below_zone_share',
    'zone_center_close_crossings', 'last_touch_age_bars',
    'near_range_0p25_atr_share', 'near_range_0p5_atr_share', 'near_range_1atr_share',
    'near_range_0p5_last_half_minus_first_half',
    'touch_last_half_minus_first_half',
    'intercontact_troughs', 'intercontact_trough_last_minus_first_atr',
    'observed_rejections', 'censored_last_contact', 'max_observed_rejection_depth_atr',
    'mean_observed_rejection_depth_atr',
    'first_touch_open_ts', 'last_touch_open_ts',
]
EVENT_COLUMNS = [
    'attempt_id', 'symbol', 'level_id', 'pre_closed_ts', 'window_bars',
    'episode_index', 'first_touch_open_ts', 'last_touch_open_ts',
    'touch_bars', 'bars_observed_after_until_next_touch_or_pre',
    'outcome', 'rejection_depth_atr', 'intercontact_trough',
]
OVERVIEW_COLUMNS = (
    ['attempt_id','symbol','level_id','pre_closed_ts'] +
    [f'{name}_{window}' for window in (12,24,48,96) for name in (
        'status','touch_episodes','touch_bars_share','last_touch_age_bars',
        'below_gap_reduction_atr','near_range_0p5_atr_share',
        'intercontact_trough_last_minus_first_atr')]
)


def _float(s, field):
    try: f = float(s)
    except (TypeError,ValueError) as e: raise ValueError(f'Invalid {field}: {s!r}') from e
    if not math.isfinite(f): raise ValueError(f'Nonfinite {field}: {s!r}')
    return f


def metadata_phase(rows):
    """Only uses identifiers and contemporaneous level values; NEVER labels."""
    out = {}
    for n,r in enumerate(rows, 2):
        for col in ('attempt_id','symbol','pre_closed_ts','zone_low','zone_high','atr_pre'):
            if col not in r: raise ValueError('phase_flags_pre.csv missing '+col)
        a = (r['attempt_id'] or '').strip(); symbol=(r['symbol'] or '').strip()
        if not a or not symbol: raise ValueError(f'Empty attempt/symbol at phase row {n}')
        ts=v2.parse_time(r['pre_closed_ts'])
        if ts%BAR_MS: raise ValueError(f'PRE not aligned for {a}')
        low=_float(r['zone_low'],'zone_low');high=_float(r['zone_high'],'zone_high');atr=_float(r['atr_pre'],'atr_pre')
        if low<=0 or high<=0 or low>high or atr<=0:
            raise ValueError(f'Invalid resistance zone/ATR in phase row {n}, {a}')
        item={'attempt_id':a,'symbol':symbol,'pre_closed_ts':ts,
              'zone_low':low,'zone_high':high,'atr_pre':atr,
              'level_id':(r.get('level_id') or '').strip()}
        if a in out and out[a]!=item:
            raise ValueError(f'Conflicting phase metadata for {a}')
        out[a]=item
    if not out: raise ValueError('Empty phase source')
    return out


def metadata_v2(rows):
    out={}
    for r in rows:
        for col in ('attempt_id','symbol','pre_closed_ts'):
            if col not in r:raise ValueError('V2 overview missing '+col)
        a=r['attempt_id'];ts=v2.parse_time(r['pre_closed_ts']);symbol=r['symbol']
        if a in out:raise ValueError('Duplicate V2 attempt '+a)
        statuses=[r.get(f'status_{n}') for n in WINDOWS]
        if any(x!='COMPLETE_PRE' for x in statuses):
            raise ValueError(f'V2 incomplete for {a}: {statuses}')
        out[a]=(symbol,ts)
    if not out:raise ValueError('Empty V2 overview')
    return out


def match_attempts(phase, overview):
    missing=sorted(set(overview)-set(phase));extra=sorted(set(phase)-set(overview))
    conflicts=[]
    for a,(sym,ts) in overview.items():
        if a in phase and (sym,ts)!=(phase[a]['symbol'],phase[a]['pre_closed_ts']):
            conflicts.append(a)
    return {'missing_phase':missing,'extra_phase':extra,'conflicts':conflicts}


def _zone_close_position(close, zl, zh, atr):
    if close<zl:return (close-zl)/atr
    if close>zh:return (close-zh)/atr
    return 0.0


def _range_distance(bar, zl, zh):
    if bar['high']<zl:return zl-bar['high']
    if bar['low']>zh:return bar['low']-zh
    return 0.0


def touch_episodes(bars, zl, zh):
    episodes=[];ongoing=None
    for i,b in enumerate(bars):
        touch=(b['high']>=zl and b['low']<=zh)
        if touch:
            if ongoing is None:ongoing={'start':i,'end':i}
            else:ongoing['end']=i
        elif ongoing is not None:
            episodes.append(ongoing);ongoing=None
    if ongoing is not None:episodes.append(ongoing)
    return episodes


def describe_window(bars, meta, window):
    """bars are exactly consecutive, fully CLOSED before PRE, in chronological order."""
    if len(bars)!=window:raise ValueError('Non-full window')
    pre=meta['pre_closed_ts'];start=pre-window*BAR_MS
    for i,b in enumerate(bars):
        if b['open_ts']!=start+i*BAR_MS or b['close_ts']!=start+(i+1)*BAR_MS:
            raise ValueError('Gap or POST/forming candle supplied to describe_window')
    zl,zh,atr=meta['zone_low'],meta['zone_high'],meta['atr_pre']
    touches=[b['high']>=zl and b['low']<=zh for b in bars]
    inside=[zl<=b['close']<=zh for b in bars]
    above=[b['close']>zh for b in bars];below=[b['close']<zl for b in bars]
    distances=[_range_distance(b,zl,zh)/atr for b in bars]
    near025=[d<=0.25+1e-12 for d in distances]
    near05=[d<=0.50+1e-12 for d in distances]
    near1=[d<=1+1e-12 for d in distances]
    eps=touch_episodes(bars,zl,zh)
    centers=[];mid=(zl+zh)/2
    for b in bars:
        if b['close']>mid: centers.append(1)
        elif b['close']<mid: centers.append(-1)
        else:centers.append(0)
    # Exact-middle closes are neutral, not invented directional flips.
    crossings=sum(centers[i]*centers[i-1]<0 for i in range(1,len(centers)))
    first_gap=max(0,zl-bars[0]['close'])/atr
    last_gap=max(0,zl-bars[-1]['close'])/atr
    half=window//2
    episode_rows=[];intertroughs=[];known_rejections=[];censored=False
    for i,ep in enumerate(eps):
        following_end=eps[i+1]['start'] if i+1<len(eps) else window
        tail=bars[ep['end']+1:following_end]
        has_followup=bool(tail)
        depth=max(0,zl-min(b['low'] for b in tail))/atr if has_followup else ''
        trough=min(b['low'] for b in tail) if has_followup and i+1<len(eps) else ''
        if has_followup:
            known_rejections.append(depth)
            if trough!='':intertroughs.append(trough)
        elif i==len(eps)-1:censored=True
        episode_rows.append({
            'episode_index':i+1,
            'first_touch_open_ts':bars[ep['start']]['open_ts'],
            'last_touch_open_ts':bars[ep['end']]['open_ts'],
            'touch_bars':ep['end']-ep['start']+1,
            'bars_observed_after_until_next_touch_or_pre':len(tail),
            'outcome':'PRE_OBSERVED' if has_followup else 'CENSORED_AT_PRE_OR_ADJACENT_TOUCH',
            'rejection_depth_atr':depth,'intercontact_trough':trough,
        })
    item={
        'attempt_id':meta['attempt_id'],'symbol':meta['symbol'],'level_id':meta['level_id'],
        'pre_closed_ts':pre,'window_bars':window,'status':'COMPLETE_PRE',
        'zone_low':zl,'zone_high':zh,'zone_mid':mid,'atr_pre':atr,
        'first_candle_open_ts':bars[0]['open_ts'],'last_candle_close_ts':bars[-1]['close_ts'],
        'first_close_relative_zone_atr':_zone_close_position(bars[0]['close'],zl,zh,atr),
        'last_close_relative_zone_atr':_zone_close_position(bars[-1]['close'],zl,zh,atr),
        'start_below_zone_gap_atr':first_gap,'end_below_zone_gap_atr':last_gap,
        'below_gap_reduction_atr':first_gap-last_gap,
        'touch_bars':sum(touches),'touch_bars_share':sum(touches)/window,
        'touch_episodes':len(eps),
        'close_inside_zone_bars':sum(inside),'close_inside_zone_share':sum(inside)/window,
        'close_above_zone_share':sum(above)/window,'close_below_zone_share':sum(below)/window,
        'zone_center_close_crossings':crossings,
        'last_touch_age_bars':window-1-eps[-1]['end'] if eps else '',
        'near_range_0p25_atr_share':sum(near025)/window,
        'near_range_0p5_atr_share':sum(near05)/window,
        'near_range_1atr_share':sum(near1)/window,
        'near_range_0p5_last_half_minus_first_half':(sum(near05[half:])/(window-half)-sum(near05[:half])/half),
        'touch_last_half_minus_first_half':(sum(touches[half:])/(window-half)-sum(touches[:half])/half),
        'intercontact_troughs':len(intertroughs),
        'intercontact_trough_last_minus_first_atr':(intertroughs[-1]-intertroughs[0])/atr if len(intertroughs)>=2 else '',
        'observed_rejections':len(known_rejections),'censored_last_contact':int(censored),
        'max_observed_rejection_depth_atr':max(known_rejections) if known_rejections else '',
        'mean_observed_rejection_depth_atr':sum(known_rejections)/len(known_rejections) if known_rejections else '',
        'first_touch_open_ts':bars[eps[0]['start']]['open_ts'] if eps else '',
        'last_touch_open_ts':bars[eps[-1]['end']]['open_ts'] if eps else '',
    }
    for r in episode_rows:r.update({k:item[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts','window_bars')})
    return item,episode_rows


def analyze(phase,overview,candles):
    match=match_attempts(phase,overview)
    if match['missing_phase'] or match['conflicts']:raise ValueError('Phase join mismatch: '+str(match))
    rows=[];events=[];summaries=[]
    for a,(symbol,pre) in sorted(overview.items()):
        m=phase[a];cs=candles.get(symbol);per={}
        for n in WINDOWS:
            expected=[pre-n*BAR_MS+i*BAR_MS for i in range(n)]
            selected=[cs[t] for t in expected if cs is not None and t in cs]
            if len(selected)!=n:
                row={k:m.get(k,'') for k in ('attempt_id','symbol','level_id','pre_closed_ts','zone_low','zone_high','atr_pre')}
                row.update({'window_bars':n,'status':'MISSING_OR_GAPPED_OHLC'})
            else:
                row,ev=describe_window(selected,m,n);events.extend(ev)
            rows.append(row);per[n]=row
        summary={k:m[k] for k in ('attempt_id','symbol','level_id','pre_closed_ts')}
        for n in WINDOWS:
            for name in ('status','touch_episodes','touch_bars_share','last_touch_age_bars',
                         'below_gap_reduction_atr','near_range_0p5_atr_share',
                         'intercontact_trough_last_minus_first_atr'):
                summary[f'{name}_{n}']=per[n].get(name,'')
        summaries.append(summary)
    return rows,events,summaries


def write_csv(path,rows,cols):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader()
        for r in rows:
            w.writerow({k:round(v,8) if isinstance(v,float) else v for k,v in r.items() if k in cols})


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--plan',action='store_true');g.add_argument('--run',action='store_true')
    p.add_argument('--phase-source',type=Path,default=DEFAULT_PHASE)
    p.add_argument('--v2-source',type=Path,default=DEFAULT_V2)
    p.add_argument('--candles-dir',type=Path,default=DEFAULT_CANDLES)
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    p.add_argument('--timestamp-kind',choices=('open','close'),default=None)
    args=p.parse_args(argv)
    for path in (args.phase_source,args.v2_source):
        if not path.is_file():p.error(f'Missing source {path}')
    try:
        phase=metadata_phase(v2.read_csv(args.phase_source))
        overview=metadata_v2(v2.read_csv(args.v2_source))
    except (ValueError,KeyError) as e:p.error(str(e))
    join=match_attempts(phase,overview)
    symbols=sorted(set(s for s,_ in overview.values()))
    sources={};candles={};errors={}
    for symbol in symbols:
        f=args.candles_dir/f'{symbol}_30m.csv'
        if not f.is_file():errors[symbol]='MISSING_FILE';continue
        try:
            cs,schema=v2.load_candles(f,args.timestamp_kind)
            candles[symbol]=cs
            sources[symbol]={'file':str(f),'bars':len(cs),'time_kind':schema['time_kind'],'time_column':schema['time_col']}
        except (ValueError,KeyError) as e:errors[symbol]=str(e)
    plan={'attempts':len(overview),'phase_attempts':len(phase),'symbols':len(symbols),
          'loaded_symbols':len(candles),'join':{'missing_phase':join['missing_phase'],
          'conflicts':join['conflicts'],'extra_phase_count':len(join['extra_phase'])},
          'source_phase':str(args.phase_source),'source_v2':str(args.v2_source),
          'candles_dir':str(args.candles_dir),'output':str(args.output),
          'schema_errors':errors,'loaded_candles':sources,
          'timestamp_contract':'open+30m <= pre_closed_ts',
          'read_only_inputs':True,'no_live':True,'no_dashboard_changes':True,'no_labels_used':True}
    print('HLL_RESISTANCE_PRE_PLAN '+json.dumps(plan,sort_keys=True,ensure_ascii=False))
    if args.plan:return 0
    if errors or join['missing_phase'] or join['conflicts']:
        print('HLL_RESISTANCE_PRE_ABORT source mismatch or invalid OHLC',flush=True);return 2
    try:rows,episodes,overview_rows=analyze(phase,overview,candles)
    except ValueError as e:
        print('HLL_RESISTANCE_PRE_ABORT '+str(e),flush=True);return 2
    args.output.mkdir(parents=True,exist_ok=True)
    write_csv(args.output/'resistance_windows_pre.csv',rows,COLUMNS)
    write_csv(args.output/'resistance_contact_episodes_pre.csv',episodes,EVENT_COLUMNS)
    write_csv(args.output/'resistance_attempts_overview_pre.csv',overview_rows,OVERVIEW_COLUMNS)
    statuses=dict(Counter(r['status'] for r in rows))
    manifest={
        'version':'HLL_RESISTANCE_INTERACTION_PRE_V3','attempts':len(overview),'windows':WINDOWS,
        'rows':len(rows),'contact_episode_rows':len(episodes),'statuses':statuses,
        'inputs_sha256':{'phase':sha256(args.phase_source),'v2':sha256(args.v2_source),
                         'candles':{s:sha256(args.candles_dir/f'{s}_30m.csv') for s in symbols}},
        'timestamp_kind':args.timestamp_kind,'source_paths':plan,'uses_post':False,
        'uses_visual_labels':False,'creates_trading_signals':False,'modifies_live':False,'modifies_dashboard':False,
        'methodological_notes':[
            'Candle high/low touch indicates overlap, not the intrabar order of events.',
            'Contact episodes merge consecutive intersecting 30m candles; episodes are not independent trades.',
            'A near-range band means candle range is within N ATR of [zone_low,zone_high]. Bands are descriptive, not filters.',
            'Rejections observed only until the following contact episode or PRE; last contact may be censored.',
            'Intercontact troughs require at least two contacts; a trend in troughs requires at least three contacts.',
            'Candle POST and forming candle excluded; all windows taken from V2 causal PRE.',
            'Overlapping attempts, windows, and symbols are not independent statistical units.',
            'Use no visual labels to compute features, choose chains, or classify examples.',
        ],
    }
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('HLL_RESISTANCE_PRE_DONE '+json.dumps({'attempts':len(overview),'window_rows':len(rows),
          'contact_episode_rows':len(episodes),'statuses':statuses,'output':str(args.output)},sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
