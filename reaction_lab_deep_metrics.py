"""Causal microstructure/volatility features + separated forward outcomes.

All as-of inputs must be *already frozen* <= known_ts. No Redis/current market
queries. Missing sources remain None; none of these features veto REACTIONs.
"""
from __future__ import annotations
import math
import statistics

MINUTE=60_000


def num(v):
    try:
        f=float(v)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError,OverflowError):
        return None


def safe_ratio(a,b):
    return a/b if a is not None and b not in (None,0) else None


def candle_tail(data, known_ts, minute=MINUTE):
    """Reject open/future bars, keep only a contiguous trailing sequence."""
    latest={}
    for r in data or []:
        try:
            ts=int(r['timestamp'])
            o,h,l,c,v=[float(r[k]) for k in ('open','high','low','close','volume')]
        except (KeyError,ValueError,TypeError,OverflowError):
            continue
        if ts+minute>known_ts:continue
        if any(not math.isfinite(x) for x in (o,h,l,c,v)):continue
        if min(o,h,l,c)<=0 or v<0 or l>min(o,c) or h<max(o,c):continue
        latest[ts]={'timestamp':ts,'open':o,'high':h,'low':l,'close':c,'volume':v}
    latest_expected=(int(known_ts)//minute-1)*minute
    if latest_expected not in latest:return []
    out=[];ts=latest_expected
    while ts in latest:
        out.append(latest[ts]);ts-=minute
    return out[::-1]


def pct(a,b):
    return (a/b-1)*100 if a is not None and b is not None and b>0 else None


def atr(bars,window=14):
    if len(bars)<window+1:return None
    trail=[]
    for p,b in zip(bars[-window-1:-1],bars[-window:]):
        trail.append(max(b['high']-b['low'],abs(b['high']-p['close']),abs(b['low']-p['close'])))
    return sum(trail)/window


def realized_vol(bars, minutes):
    if len(bars)<minutes+1:return None
    v=[math.log(b['close']/p['close']) for p,b in zip(bars[-minutes-1:-1],bars[-minutes:])]
    return statistics.stdev(v)*100*math.sqrt(minutes) if len(v)>=2 else None


def causal_features(event,context,candles,known, density=None, telemetry=None):
    side=str(event.get('signal') or '').upper()
    if side not in ('LONG','SHORT'):raise ValueError('Invalid side')
    sign=1 if side=='LONG' else -1
    bars=candle_tail(candles.get('1m'),known)
    fields={'deep_research_version':1, 'deep_asof_bars_1m':len(bars),
            'deep_1m_latest_ts':bars[-1]['timestamp'] if bars else None,
            'deep_causal_input_status':'READY' if len(bars)>=31 else 'PARTIAL'}
    raw_level=num(event.get('swing_price')) or num(event.get('pivot_price'))
    # Scanner-selected departure is as-of and is NOT recomputed using later bars.
    departure_ts=num(event.get('departure_timestamp'))
    departure_pct=num(event.get('max_departure_pct'))
    actionable_ts=num(event.get('actionable_timestamp'))
    if departure_ts is not None and actionable_ts is not None and departure_ts>=actionable_ts:
        duration=(departure_ts-actionable_ts)/MINUTE
    else:duration=None
    fields.update(departure_duration_min=duration,
                  departure_speed_pct_per_min=safe_ratio(departure_pct,duration),
                  departure_atr_multiple=None,
                  departure_efficiency=None,
                  departure_directional_bar_share=None,
                  departure_relative_volume=None)
    # Departure price/swing level already in scanner; quantify how the as-of
    # confirmed->departure segment got there, never include the return after retest.
    if bars and departure_ts is not None and actionable_ts is not None:
        segment=[b for b in bars if actionable_ts<=b['timestamp']<=departure_ts]
        if len(segment)>=2 and all(b['timestamp']-a['timestamp']==MINUTE for a,b in zip(segment,segment[1:])):
            moves=[(b['close']-a['close'])/a['close'] for a,b in zip(segment,segment[1:])]
            net=abs(segment[-1]['close']/segment[0]['close']-1)
            path=sum(abs(v) for v in moves)
            fields['departure_efficiency']=min(1.0,net/path) if path>0 else None
            fields['departure_directional_bar_share']=sum(sign*v>0 for v in moves)/len(moves)
            previous=[b['volume'] for b in bars if b['timestamp']<actionable_ts][-30:]
            if len(previous)>=10 and statistics.median(previous)>0:
                fields['departure_relative_volume']=(sum(b['volume'] for b in segment)/len(segment))/statistics.median(previous)
    if bars:
        entry=bars[-1];close=entry['close']
        wick_down=min(entry['open'],close)-entry['low']
        wick_up=entry['high']-max(entry['open'],close)
        span=entry['high']-entry['low']
        fields['retest_rejection_wick_share']=safe_ratio(wick_down if side=='LONG' else wick_up,span)
        fields['retest_close_strength']=safe_ratio(close-entry['low'] if side=='LONG' else entry['high']-close,span)
        fields['retest_candle_body_share']=safe_ratio(abs(close-entry['open']),span)
        fields['retest_volume']=entry['volume']
        fields['retest_penetration_pct']=num(event.get('penetration_pct'))
        fields['retest_distance_pct']=num(event.get('retest_distance_pct'))
        fields['retest_age_min']=num(event.get('retest_age_min'))
        fields['retest_level_distance_atr']=None
        fields['retest_approach_5m_speed_pct_min']=None
        fields['retest_approach_15m_efficiency']=None
        if len(bars)>=6:
            # Signed as direction of approach TOWARD swing: + means approaching
            start=bars[-6]['close']
            fields['retest_approach_5m_speed_pct_min']=-sign*(close/start-1)*100/5
        if len(bars)>=16:
            m=[math.log(b['close']/a['close']) for a,b in zip(bars[-16:-1],bars[-15:])]
            displacement=abs(math.log(close/bars[-16]['close']))
            traversed=sum(abs(x) for x in m)
            fields['retest_approach_15m_efficiency']=min(1.0, displacement/traversed) if traversed>0 else None
        value=atr(bars)
        fields['atr_1m_pct']=100*value/close if value is not None else None
        fields['atr_1m_abs']=value
        atr_samples=[]
        if len(bars)>=45:
            for j in range(max(15,len(bars)-200),len(bars)):
                a=atr(bars[:j+1])
                if a is not None and a>0:atr_samples.append(100*a/bars[j]['close'])
        fields['atr_1m_percentile_200']=(100*sum(x<=fields['atr_1m_pct'] for x in atr_samples)/len(atr_samples)
            if atr_samples and fields['atr_1m_pct'] is not None else None)
        fields['retest_level_distance_atr']=abs(close-raw_level)/value if raw_level and value else None
        fields['departure_atr_multiple']=departure_pct/(fields['atr_1m_pct']) if departure_pct is not None and fields['atr_1m_pct'] else None
        for horizon in (5,15,30,60):fields[f'realized_vol_{horizon}m_pct']=realized_vol(bars,horizon)
        # Relative to previous 1h 1m return magnitude; never uses a future value.
        if len(bars)>=61:
            prior=[abs(math.log(b['close']/a['close'])) for a,b in zip(bars[-61:-1],bars[-60:])]
            latest=abs(math.log(bars[-1]['close']/bars[-2]['close']))
            ref=statistics.median(prior[:-1]) if len(prior)>2 else None
            fields['retest_volatility_shock']=latest/ref if ref and ref>0 else None
        prior_vol=[b['volume'] for b in bars[-31:-1]]
        fields['retest_volume_relative_30m']=entry['volume']/statistics.median(prior_vol) if len(prior_vol)>=20 and statistics.median(prior_vol)>0 else None
    # Existing as-of cross-sectional context: do NOT silently infer a fresh slope.
    for name in ('market_breadth_1h','market_breadth_4h','breadth_delta_1h','breadth_delta_4h',
                 'btc_return_delta_1h','btc_return_delta_4h','nearest_opposing_room_pct',
                 'sector_strength_vs_btc_4h','side_adjusted_strength_vs_btc_4h'):
        fields['deep_'+name]=num(context.get(name))
    fields['deep_market_flow_1h']=context.get('Market alignment 1h')
    fields['deep_market_flow_4h']=context.get('Market alignment 4h')
    fields['deep_market_flow_divergence']=('DIVERGENT' if fields['deep_market_flow_1h'] not in (None,'UNKNOWN') and fields['deep_market_flow_4h'] not in (None,'UNKNOWN') and fields['deep_market_flow_1h']!=fields['deep_market_flow_4h'] else 'ALIGNED' if fields['deep_market_flow_1h']==fields['deep_market_flow_4h'] and fields['deep_market_flow_1h'] not in (None,'UNKNOWN') else None)
    fields.update(density or {})
    # No taker/bid-ask/OI/funding values invented from candle volume.
    for key in ('telemetry_observed_ms','telemetry_age_sec','oi_contracts','oi_change_5m_pct','oi_change_15m_pct','funding_rate',
                'taker_buy_volume_5m','taker_sell_volume_5m','taker_delta_5m','cvd_15m',
                'taker_buy_ratio_5m','orderbook_spread_bps','orderbook_imbalance_5',
                'orderbook_bid_depth_usdt','orderbook_ask_depth_usdt','liquidations_15m_usdt',
                'sector_reaction_density_15m'):
        fields.setdefault(key,None)
    if telemetry:
        # Keys already verified by the immutable telemetry sidecar (as-of only).
        for key in list(fields):
            if key in telemetry and key not in ('deep_research_version','deep_asof_bars_1m','deep_causal_input_status'):
                fields[key]=telemetry[key]
    fields['microstructure_status']=('OBSERVED' if telemetry and all(fields.get(k) is not None for k in ('oi_contracts','funding_rate','taker_buy_ratio_5m','orderbook_spread_bps','liquidations_15m_usdt'))
       else 'PARTIAL' if telemetry else 'NOT_CAPTURED_AT_SIGNAL')
    return fields


def forward_metrics(path,side,retest_close,retest_ts,horizon=360):
    """Consecutive complete closed 1m bars. OPEN next bar = executable proxy.

    For same-bar TP+SL, result is AMBIGUOUS (SL_FIRST conservative). Values
    across horizon are explicitly unavailable until that horizon is complete.
    """
    if side not in ('LONG','SHORT'):return {}
    out={}
    bars=[]
    for i,r in enumerate((path or [])[:horizon]):
        try:ts,o,hi,lo,c=r;ts=int(ts);o,hi,lo,c=map(float,(o,hi,lo,c))
        except (TypeError,ValueError,OverflowError):break
        if ts!=int(retest_ts)+(i+1)*MINUTE or not (0<lo<=min(o,c)<=max(o,c)<=hi):break
        bars.append((ts,o,hi,lo,c))
    out['valid_path_minutes']=len(bars)
    if not bars:return out
    entry=bars[0][1]
    out.update(entry_next_open=entry,entry_next_open_ts=bars[0][0],entry_retest_close=num(retest_close))
    direction=1 if side=='LONG' else -1
    tp_levels=(1.,2.,3.,5.)
    for window in (30,60,120,180,360):
        if len(bars)<window:
            out[f'outcome_{window}m_status']='INCOMPLETE'
            continue
        segment=bars[:window]
        gains=[];losses=[]
        for _,o,h,l,c in segment:
            g=100*(h/entry-1) if direction==1 else 100*(1-l/entry)
            d=100*(1-l/entry) if direction==1 else 100*(h/entry-1)
            gains.append(g);losses.append(d)
        out[f'outcome_{window}m_status']='COMPLETE'
        out[f'mfe_entry_{window}m_pct']=max(0.,max(gains))
        out[f'mae_entry_{window}m_pct']=max(0.,max(losses))
        for target in tp_levels:
            first=next((i for i,v in enumerate(gains) if v>target),None)
            tag=str(int(target))
            out[f'time_to_tp_{tag}_{window}m']=first+1 if first is not None else None
            # OHLC cannot order the TP crossing and adverse wick in the same
            # bar. Separate strictly prior MAE from conservative inclusive MAE.
            out[f'mae_before_tp_{tag}_{window}m_pct']=(max([0.]+losses[:first]) if first is not None else None)
            out[f'mae_through_tp_bar_{tag}_{window}m_pct']=(max([0.]+losses[:first+1]) if first is not None else None)
        for tp,sl in ((1.,.5),(2.,1.),(3.,1.),(5.,2.)):
            result='NO_HIT';minutes=None
            for i,(g,d) in enumerate(zip(gains,losses)):
                if g>tp and d>=sl: result='AMBIGUOUS_SL_FIRST';minutes=i+1;break
                if d>=sl:result='SL_FIRST';minutes=i+1;break
                if g>tp:result='TP_FIRST';minutes=i+1;break
            suffix=f'{int(tp)}tp_{str(sl).replace(".","p")}sl_{window}m'
            out[f'tp_sl_{suffix}']=result
            out[f'tp_sl_minutes_{suffix}']=minutes
    return out
