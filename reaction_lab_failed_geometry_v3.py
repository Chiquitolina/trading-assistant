#!/usr/bin/env python3
"""FAILED Geometry V3: time-aware impulse/pullback geometry, without PnL.

Depends on the unmodified V1/V2 source modules installed in the project root.
Reads the original journal/path/flow databases read-only; only writes standalone CSV
and Markdown in reports/reaction_lab/failed_geometry_v3/. Does not access Redis.
"""
from __future__ import annotations
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import reaction_lab_failed_behavior_v1 as v1
import reaction_lab_failed_behavior_v2 as v2

OUT_REL='reports/reaction_lab/failed_geometry_v3'
WINDOWS=(5,10,15,30)
THRESHOLDS=(.5,1.0)
SCAN_HORIZON=30  # From t5: recognition ends at t35 (same definition as V2)
LANDMARK=5
BREAK_MARGIN=.05
TOL=.1


def finite(v):
    try:
        z=float(v)
        return z if math.isfinite(z) else None
    except (ValueError,TypeError):return None


def median(items):
    return v1.pct(items)['median']


def is_forward_side(ev_side, first_dir):
    """First impulse price direction (+1 upward, -1 downward)."""
    return (1 if ev_side=='SHORT' else -1)*first_dir


def directed_pct(price, start, ev_side, first_dir):
    """Signed percent from a *known* reference price in first impulse direction."""
    return first_dir*v2.side_move(price,start,ev_side)


def edge_prices(bar, forward_price_sign):
    # Bar shape t,o,h,l,c. Favorable and adverse highs/lows are NOT ordered.
    return ((bar[2],bar[3]) if forward_price_sign==1 else (bar[3],bar[2]))


def race_on_closes(moves, threshold):
    """Post-observation future closes; outcomes are exclusive by timestamp."""
    first=None;both=set()
    for i,m in enumerate(moves,1):
        if m>=threshold:label='CONTINUE'
        elif m<=-threshold:label='REVERSE'
        else:continue
        both.add(label)
        if first is None:first=(label,i)
    return (first[0] if first else 'NEITHER',first[1] if first else None,len(both)==2)


def first_second_event(ev, pb_min, horizon, baseline, peak_pct, first_dir):
    """Frozen-peak breakout and baseline cross AFTER pullback confirmation.

    Distances refer to price at t5. Events crossing baseline already on the
    pullback-confirming close are not at risk for this future race.
    """
    pb=ev['bars'][pb_min-1][4]
    pb_progress=first_dir*v2.side_move(pb,baseline,ev['side'])
    if pb_progress<=0:
        return ('BASELINE_ALREADY_AT_PB',0,False)
    earlier=None;hit=set()
    for i,bar in enumerate(ev['bars'][pb_min:pb_min+horizon],1):
        v=first_dir*v2.side_move(bar[4],baseline,ev['side'])
        if v>=peak_pct+BREAK_MARGIN:now='SECOND_BREAKOUT'
        elif v<=0:now='BASELINE_CROSSED'
        else:continue
        hit.add(now)
        if earlier is None:earlier=(now,i)
    return (earlier[0] if earlier else 'NEITHER', earlier[1] if earlier else None, len(hit)==2)


def anatomical_event(ev, *, threshold=.5, scan_horizon=SCAN_HORIZON):
    """An episode is recognized using only its past at the pullback close.

    V2 pattern is the single source of truth for phase transition semantics.
    No forward outcome enters the anatomy fields or the pre-pullback matching.
    """
    seq=v2.pattern(ev,landmark=LANDMARK,horizon=scan_horizon,threshold=threshold)
    if seq is None:return None
    obs=v2.observation(ev,LANDMARK)
    firstdir={'BREAKOUT':1,'REVERSAL':-1}.get(seq['impulse_direction'])
    b=ev['bars'];base_price=obs['baseline_price']
    out={**seq,'cohort_type':'FAILED' if ev['status']=='TOUCH_FAILED' else 'MATCHED_NONFAILED',
         'date_utc':obs['date_utc'],'first_5_outside':obs['all_first_5_closes_outside'],
         'early_signed_5m_pct':obs['move_5m_break_pct'],'early_range_5m_pct':obs['range_5m_pct'],
         'initial_penetration_pct':obs['penetration_at_fail_pct'],
         'baseline_m5_price':base_price,'post_pb_available_bars':None,
         'pb_confirmed':seq['pullback_min'] is not None,
         'geometry_known_at_pb':False,
         'source_status':seq['resolution']}
    if not firstdir:
        return out
    impulse=seq['impulse_min'];peak_min=seq['peak_min'];pb=seq['pullback_min']
    out.update(first_direction_sign=firstdir,
               impulse_close_price=b[impulse-1][4] if impulse else None,
               frozen_peak_price=b[peak_min-1][4] if peak_min else None,
               minutes_to_impulse=impulse-LANDMARK if impulse else None,
               minutes_impulse_to_peak=peak_min-impulse if impulse and peak_min else None)
    if pb is None:return out
    price=b[pb-1][4]
    peak_price=b[peak_min-1][4]
    peak=seq['impulse_peak_pct']
    progress=firstdir*v2.side_move(price,base_price,ev['side'])
    room_peak=directed_pct(peak_price,price,ev['side'],firstdir)
    room_base=directed_pct(base_price,price,ev['side'],-firstdir)
    swing=v1.positive(ev['meta'].get('swing_price'))
    dist_swing=v1.break_distance(price,swing,ev['side']) if swing else None
    out.update(geometry_known_at_pb=True, pb_close_price=price,
       pullback_progress_from_m5_pct=progress,
       pb_retrace_from_frozen_peak_pct=peak-progress,
       pb_retrace_fraction_pct=100*(peak-progress)/peak if peak>0 else None,
       room_to_frozen_peak_pct=room_peak,
       room_to_frozen_peak_plus_margin_pct=room_peak+BREAK_MARGIN*base_price/price,
       room_to_m5_baseline_pct=room_base,
       pb_already_crossed_m5=progress<=0,
       pb_distance_to_original_swing_pct=dist_swing,
       pb_distance_outside_swing_band_pct=(dist_swing-TOL if dist_swing is not None else None),
       pb_reclaimed_original_swing=(dist_swing<=0 if dist_swing is not None else None),
       pb_reentered_original_band=(dist_swing<=TOL if dist_swing is not None else None),
       minutes_impulse_to_pullback=pb-impulse,
       minutes_peak_to_pullback=pb-peak_min,
       pb_close_change_last_1m_pct=directed_pct(price,b[pb-2][4],ev['side'],firstdir)
           if pb>=2 else None,
       pb_last_candle_range_pct=(b[pb-1][2]-b[pb-1][3])/price*100,
       post_pb_available_bars=max(0,len(b)-pb),
       risk_set_future_competition=progress>0)
    return out


def post_pullback_window(ev, episode, horizon):
    """Uses only bars strictly AFTER the pullback-confirming close.

    Incomplete horizon => censor, even if a favorable condition was seen.
    Does not invent execution; OHLC extrema have unknown intrabar ordering.
    """
    if not episode or not episode.get('geometry_known_at_pb'):
        return None
    pb=int(episode['pullback_min']);b=ev['bars']
    available=max(0,len(b)-pb)
    full=available>=horizon
    following=b[pb:pb+min(horizon,available)]
    price=episode['pb_close_price']
    dr=int(episode['first_direction_sign'])
    sign=is_forward_side(ev['side'],dr)
    end_moves=[directed_pct(x[4],price,ev['side'],dr) for x in following]
    favorable=[];adverse=[]
    for bar in following:
        fp,ap=edge_prices(bar,sign)
        favorable.append(directed_pct(fp,price,ev['side'],dr))
        adverse.append(directed_pct(ap,price,ev['side'],dr))
    threshold_races={}
    for t in (.25,.5,1.0):
        kind,delay,both=race_on_closes(end_moves,t)
        name={.25:'0p25',.5:'0p5',1.0:'1p0'}[t]
        threshold_races[f'close_first_{name}']=kind if full else 'CENSORED'
        threshold_races[f'first_{name}_delay_min']=delay if full else None
        threshold_races[f'close_both_{name}']=both if full else None
    first_outcome,first_t,both=race_on_closes(end_moves,.5)
    swing=v1.positive(ev['meta'].get('swing_price'))
    first_swing_reenter=None
    if swing is not None and episode['pb_distance_to_original_swing_pct'] is not None and episode['pb_distance_to_original_swing_pct']>TOL:
        for i,bar in enumerate(following,1):
            if v1.break_distance(bar[4],swing,ev['side'])<=TOL:
                first_swing_reenter=i;break
    breakout,break_t,both_ends=first_second_event(
        ev,pb,min(horizon,available),episode['baseline_m5_price'],
        episode['impulse_peak_pct'],dr)
    return dict(
       touch_key=ev['key'],symbol=ev['symbol'],cohort_type=episode['cohort_type'],
       original_side=ev['side'],impulse_direction=episode['impulse_direction'],
       observation_type=ev['observation'],date_utc=episode['date_utc'],
       threshold_pct=episode['threshold_pct'],scan_horizon=episode['horizon'],
       post_pb_horizon=horizon,pullback_min=pb,available_post_pb=available,
       full_coverage=full,censored=not full,
       risk_set_future_competition=bool(episode['risk_set_future_competition']),
       first_5_outside=episode['first_5_outside'],
       penetration_at_fail_pct=episode['initial_penetration_pct'],
       early_signed_5m_pct=episode['early_signed_5m_pct'],
       minutes_impulse_to_pullback=episode['minutes_impulse_to_pullback'],
       pb_time_bucket=('1-2m' if episode['minutes_impulse_to_pullback']<=2 else
                       '3-5m' if episode['minutes_impulse_to_pullback']<=5 else '6m+'),
       peak_room_bucket=('<0.25%' if episode['room_to_frozen_peak_pct']<.25 else
                         '0.25-0.50%' if episode['room_to_frozen_peak_pct']<.5 else
                         '0.50-1.00%' if episode['room_to_frozen_peak_pct']<1 else '>=1.00%'),
       initial_penetration_bucket=(v1.penetration_bucket(episode['initial_penetration_pct'])
             if episode['initial_penetration_pct'] is not None else 'MISSING'),
       distance_to_peak_at_pb_pct=episode['room_to_frozen_peak_pct'],
       distance_to_baseline_at_pb_pct=episode['room_to_m5_baseline_pct'],
       pb_retrace_fraction_pct=episode['pb_retrace_fraction_pct'],
       pb_distance_to_swing_pct=episode['pb_distance_to_original_swing_pct'],
       post_pb_close_change_pct=end_moves[-1] if full and end_moves else None,
       post_pb_mfe_pct=max([0]+favorable) if full else None,
       post_pb_mae_pct=max([0]+[-x for x in adverse]) if full else None,
       **threshold_races,
       swing_reentry_at_risk=(episode['pb_distance_to_original_swing_pct'] is not None and episode['pb_distance_to_original_swing_pct']>TOL),
       future_swing_reentry_min=first_swing_reenter if full else None,
       future_swing_reentry_status=('REENTERED' if first_swing_reenter is not None else 'NO_REENTRY') if full else 'CENSORED',
       frozen_peak_vs_baseline_race=breakout if full else 'CENSORED',
       frozen_peak_race_delay_min=break_t if full else None,
       both_peak_and_baseline=both_ends if full else None,
       # Store observed hits separately even for censored trajectories; NEVER
       # treat no-hit from an incomplete path as a complete negative.
       observed_closes_post_pb=len(end_moves),
       first_close_0p5_observed=first_outcome)


def paired_strict(matches,events_map,*, threshold=.5, horizon=30):
    """Re-apply both-side coverage at the pair level, never independent filters.

    Stage A: full t5+H observation BOTH members => first-impulse comparisons.
    Stage B: both show PB while episode is recognisable => geometry comparison
    conditional on reaching PB. This is selected-on-post-t5 and not an
    unconditional causal treatment comparison.
    """
    out=[]
    for pair in matches:
        f=events_map.get(pair['failed_key']);c=events_map.get(pair['control_key'])
        if f is None or c is None:continue
        a=anatomical_event(f,threshold=threshold,scan_horizon=horizon)
        b=anatomical_event(c,threshold=threshold,scan_horizon=horizon)
        if not a or not b:continue
        full_pair=bool(a['full_coverage'] and b['full_coverage'])
        both_pb=full_pair and a['pb_confirmed'] and b['pb_confirmed']
        fa=post_pullback_window(f,a,30) if a['pb_confirmed'] else None
        ca=post_pullback_window(c,b,30) if b['pb_confirmed'] else None
        both_postpb=bool(both_pb and fa and ca and fa['full_coverage'] and ca['full_coverage'])
        same_dir=bool(both_pb and a['impulse_direction']==b['impulse_direction'])
        both_risk=bool(both_postpb and a['risk_set_future_competition'] and b['risk_set_future_competition'])
        out.append(dict(failed_key=f['key'],control_key=c['key'],
             original_side=f['side'],date_utc=a['date_utc'],
             match_score=pair['match_score'],same_symbol=pair['same_symbol'],
             threshold_pct=threshold,scan_horizon=horizon,
             pair_full_initial=full_pair,pair_both_pb=bool(both_pb),
             pair_full_post_pb_30m=both_postpb,
             pair_same_impulse_direction=same_dir,
             pair_both_future_risk=both_risk,
             pair_same_direction_both_risk=bool(both_risk and same_dir),
             failed_impulse=a['impulse_direction'] if full_pair else 'CENSORED',
             control_impulse=b['impulse_direction'] if full_pair else 'CENSORED',
             failed_has_pb=a['pb_confirmed'] if full_pair else None,
             control_has_pb=b['pb_confirmed'] if full_pair else None,
             failed_second_race=(fa['frozen_peak_vs_baseline_race'] if both_postpb else 'CENSORED'),
             control_second_race=(ca['frozen_peak_vs_baseline_race'] if both_postpb else 'CENSORED'),
             failed_peak_room_pct=(a['room_to_frozen_peak_pct'] if both_pb else None),
             control_peak_room_pct=(b['room_to_frozen_peak_pct'] if both_pb else None)))
    return out


def group_results(rows,columns):
    groups=defaultdict(list)
    for r in rows:
        groups[tuple(r[c] for c in columns)].append(r)
    result=[]
    for key,items in sorted(groups.items(),key=lambda t:str(t[0])):
        d=dict(zip(columns,key))
        all_n=len(items)
        full=[r for r in items if r['full_coverage']]
        at_risk=[r for r in full if r['risk_set_future_competition']]
        d.update(n_all=all_n,n_full=len(full),n_censored=all_n-len(full),
            n_future_risk=len(at_risk),
            median_peak_room_pct=median(r['distance_to_peak_at_pb_pct'] for r in full),
            median_baseline_room_pct=median(r['distance_to_baseline_at_pb_pct'] for r in full),
            median_post_pb_mfe_pct=median(r['post_pb_mfe_pct'] for r in full),
            post_pb_mfe_p10_pct=v1.pct(r['post_pb_mfe_pct'] for r in full)['p10'],
            post_pb_mfe_p90_pct=v1.pct(r['post_pb_mfe_pct'] for r in full)['p90'],
            median_post_pb_mae_pct=median(r['post_pb_mae_pct'] for r in full),
            post_pb_mae_p10_pct=v1.pct(r['post_pb_mae_pct'] for r in full)['p10'],
            post_pb_mae_p90_pct=v1.pct(r['post_pb_mae_pct'] for r in full)['p90'],
            median_post_pb_end_pct=median(r['post_pb_close_change_pct'] for r in full),
            pct_forward_half_first=(100*sum(r['close_first_0p5']=='CONTINUE' for r in full)/len(full) if full else None),
            pct_first_quarter_continue=(100*sum(r['close_first_0p25']=='CONTINUE' for r in full)/len(full) if full else None),
            pct_first_one_continue=(100*sum(r['close_first_1p0']=='CONTINUE' for r in full)/len(full) if full else None),
            pct_reverse_half_first=(100*sum(r['close_first_0p5']=='REVERSE' for r in full)/len(full) if full else None),
            pct_first_neither=(100*sum(r['close_first_0p5']=='NEITHER' for r in full)/len(full) if full else None),
            pct_second_first_risk=(100*sum(r['frozen_peak_vs_baseline_race']=='SECOND_BREAKOUT' for r in at_risk)/len(at_risk) if at_risk else None),
            pct_baseline_first_risk=(100*sum(r['frozen_peak_vs_baseline_race']=='BASELINE_CROSSED' for r in at_risk)/len(at_risk) if at_risk else None),
            median_first_half_delay_min=median(r['first_0p5_delay_min'] for r in full),
            n_swing_reentry_at_risk=sum(r['swing_reentry_at_risk'] for r in full),
            pct_swing_reentry_risk=(100*sum(r['future_swing_reentry_status']=='REENTERED' for r in full) /sum(r['swing_reentry_at_risk'] for r in full) if any(r['swing_reentry_at_risk'] for r in full) else None),
            median_second_delay_min=median(r['frozen_peak_race_delay_min'] for r in at_risk))
        result.append(d)
    return result


def pair_summary(rows):
    out=[]
    groups=defaultdict(list)
    for r in rows:groups[(r['threshold_pct'],r['original_side'])].append(r)
    for (th,side),items in sorted(groups.items()):
        full=[r for r in items if r['pair_full_initial']]
        bpb=[r for r in full if r['pair_both_pb']]
        post=[r for r in bpb if r['pair_full_post_pb_30m']]
        risk=[r for r in post if r['pair_both_future_risk']]
        strict_same=[r for r in risk if r['pair_same_impulse_direction']]
        out.append(dict(threshold_pct=th,original_side=side,
          matched_pairs_all=len(items),matched_pairs_both_full_t35=len(full),
          failed_impulse_observed=sum(r['failed_impulse']!='NO_IMPULSE' for r in full),
          control_impulse_observed=sum(r['control_impulse']!='NO_IMPULSE' for r in full),
          failed_breakout_first_count=sum(r['failed_impulse']=='BREAKOUT' for r in full),
          control_breakout_first_count=sum(r['control_impulse']=='BREAKOUT' for r in full),
          failed_breakout_first_pct=100*sum(r['failed_impulse']=='BREAKOUT' for r in full)/len(full) if full else None,
          control_breakout_first_pct=100*sum(r['control_impulse']=='BREAKOUT' for r in full)/len(full) if full else None,
          failed_breakout_given_impulse_pct=(100*sum(r['failed_impulse']=='BREAKOUT' for r in full)/sum(r['failed_impulse']!='NO_IMPULSE' for r in full) if any(r['failed_impulse']!='NO_IMPULSE' for r in full) else None),
          control_breakout_given_impulse_pct=(100*sum(r['control_impulse']=='BREAKOUT' for r in full)/sum(r['control_impulse']!='NO_IMPULSE' for r in full) if any(r['control_impulse']!='NO_IMPULSE' for r in full) else None),
          both_pb_count=len(bpb),both_pb_postpb_30_full=len(post),
          both_risk_postpb_count=len(risk),both_risk_same_impulse_count=len(strict_same),
          failed_second_pct_both_risk=(100*sum(r['failed_second_race']=='SECOND_BREAKOUT' for r in risk)/len(risk) if risk else None),
          control_second_pct_both_risk=(100*sum(r['control_second_race']=='SECOND_BREAKOUT' for r in risk)/len(risk) if risk else None),
          failed_second_pct_same_direction=(100*sum(r['failed_second_race']=='SECOND_BREAKOUT' for r in strict_same)/len(strict_same) if strict_same else None),
          control_second_pct_same_direction=(100*sum(r['control_second_race']=='SECOND_BREAKOUT' for r in strict_same)/len(strict_same) if strict_same else None),
          failed_peak_room_med_pct=median(r['failed_peak_room_pct'] for r in bpb),
          control_peak_room_med_pct=median(r['control_peak_room_pct'] for r in bpb)))
    return out


def run(project):
    project=Path(project)
    journal=project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
    paths=project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
    all_events=v2.read_all_first_touches(journal,paths)
    events={e['key']:e for e in all_events}
    failed=[e for e in all_events if e['status']=='TOUCH_FAILED']
    control=[e for e in all_events if e['status'] in ('REACTION','TOUCH_INDECISIVE')]
    obs={e['key']:v2.observation(e,LANDMARK) for e in all_events}
    matches=v2.nearest_controls([obs[e['key']] for e in failed if obs[e['key']]],
                                [obs[e['key']] for e in control if obs[e['key']]])
    chosen={m['control_key'] for m in matches}
    asof,archive_status=v2.read_flow_asof(project,failed)
    outdir=project/OUT_REL
    anatomy=[];windows=[]
    for e in [*failed,*(events[k] for k in sorted(chosen))]:
        for threshold in THRESHOLDS:
            epi=anatomical_event(e,threshold=threshold)
            if epi is None:continue
            f=asof.get(e['key']) if e['status']=='TOUCH_FAILED' else None
            epi.update(flow_asof_available=f is not None,
                return_rank_4h_pct=f['return_rank_4h_pct'] if f else None,
                volume_rank_4h_pct=f['volume_rank_4h_pct'] if f else None,
                relative_volume_4h=f['relative_volume_4h'] if f else None,
                flow_group=f['flow_group'] if f else 'UNAVAILABLE_ASOF',
                sector=f['primary_sector'] if f else 'UNAVAILABLE_ASOF')
            anatomy.append(epi)
            if epi['pb_confirmed']:
                for h in WINDOWS:windows.append(post_pullback_window(e,epi,h))
    strict=[]
    for th in THRESHOLDS:strict.extend(paired_strict(matches,events,threshold=th))
    full_summaries=[]
    full_summaries.extend(group_results(windows,('cohort_type','original_side','impulse_direction','threshold_pct','post_pb_horizon')))
    for segmentation in ('pb_time_bucket','peak_room_bucket','first_5_outside','initial_penetration_bucket'):
        full_summaries.extend(group_results(windows,('cohort_type','original_side',segmentation,'threshold_pct','post_pb_horizon')))
    # Daily stratification: descriptive only; 2 dates are not an independent OOS check.
    temporal=group_results([r for r in windows if r['cohort_type']=='FAILED'],
          ('date_utc','original_side','impulse_direction','threshold_pct','post_pb_horizon'))
    paired=pair_summary(strict)
    focus_summary=[r for r in full_summaries if set(('cohort_type','original_side','impulse_direction','threshold_pct','post_pb_horizon')).issubset(r)
       and r['threshold_pct']==.5 and r['post_pb_horizon'] in (10,30)
       and ((r['original_side']=='LONG' and r['impulse_direction']=='BREAKOUT')
         or (r['original_side']=='SHORT' and r['impulse_direction']=='REVERSAL'))]
    v2.rows_to_csv(outdir/'geometry_events.csv',anatomy)
    v2.rows_to_csv(outdir/'post_pullback_windows.csv',windows)
    v2.rows_to_csv(outdir/'paired_strict.csv',strict)
    v2.rows_to_csv(outdir/'paired_summary.csv',paired)
    v2.rows_to_csv(outdir/'geometry_summary.csv',full_summaries)
    v2.rows_to_csv(outdir/'focus_hypotheses_summary.csv',focus_summary)
    v2.rows_to_csv(outdir/'daily_geometry_summary.csv',temporal)
    flow_windows=[r for r in windows if r['cohort_type']=='FAILED' and asof.get(r['touch_key'])]
    for r in flow_windows:
        f=asof[r['touch_key']];r['flow_group']=f['flow_group'];r['return_rank_bucket']=v2.flow_bucket(f['return_rank_4h_pct']);r['volume_rank_bucket']=v2.flow_bucket(f['volume_rank_4h_pct'])
    # Separate supplementary table; never assign contemporary context to history.
    flow_summaries=group_results(flow_windows,('flow_group','return_rank_bucket','original_side','threshold_pct','post_pb_horizon'))
    v2.rows_to_csv(outdir/'flow_geometry_asof.csv',flow_summaries,
        columns=list(flow_summaries[0]) if flow_summaries else
         ['flow_group','return_rank_bucket','original_side','threshold_pct','post_pb_horizon','n_all','n_full'])
    report=dict(generated_at_utc=datetime.now(timezone.utc).isoformat(),
       source_journal=str(journal),source_paths=str(paths),archive_status=archive_status,
       first_touches=len(all_events),failed=len(failed),matched_pairs=len(matches),
       failed_with_5m=sum(obs[e['key']] is not None for e in failed),
       failed_anatomy_with_pb_0p5=sum(r['cohort_type']=='FAILED' and r['threshold_pct']==.5 and r['pb_confirmed'] for r in anatomy),
       failed_complete_post_pb_30_0p5=sum(r['cohort_type']=='FAILED' and r['threshold_pct']==.5 and r['post_pb_horizon']==30 and r['full_coverage'] for r in windows),
       pair_full_t35_0p5=sum(r['threshold_pct']==.5 and r['pair_full_initial'] for r in strict),
       pair_full_both_postpb_30_0p5=sum(r['threshold_pct']==.5 and r['pair_full_post_pb_30m'] for r in strict),
       failed_flow_asof=sum(e['key'] in asof for e in failed),
       output=str(outdir),
       caveats=['No PnL, simulated fill or order execution. Geometry is descriptive.',
           'Impulse and pullback recognized with V2 CLOSE confirmation in t6..t35; future windows restart strictly after pullback close and may extend beyond t35.',
           'Bar extrema do not resolve intrabar ordering; chronological threshold races use CLOSE only.',
           'Uncompleted post-pullback windows are CENSORED, never counted as no-hit.',
           'Control matches are other first touches, paired by historical t5 known features. They are NOT random market bars.',
           'Paired conditional-on-pullback comparisons are subject to selection effects.',
           'Rankings only joined from archived market flow observed BEFORE the original event.',
           'Multiple subgroups and timestamps, small independent calendar coverage; no OOS validation.'])
    outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    lines=['# FAILED Geometry Lab V3 — geometría y cronología (descriptivo)','',
        f"- FAILED totales: **{len(failed)}**, con t5: **{report['failed_with_5m']}**.",
        f"- Primeros retesteos de control emparejados: **{len(matches)} pares**, t5+30 completos en ambos: **{report['pair_full_t35_0p5']}**.",
        f"- FAILED con retroceso confirmado tras impulso ±0.5%: **{report['failed_anatomy_with_pb_0p5']}**.",
        f"- FAILED con otros 30m completos desde el retroceso: **{report['failed_complete_post_pb_30_0p5']}**.",
        f"- Pares completos en ambos tras retroceso: **{report['pair_full_both_postpb_30_0p5']}**.",
        f"- Contexto flow causal (antes del evento): **{report['failed_flow_asof']}**.",
        '', '## Diseño',
        '- `t=5` fija el precio base del inicio del estudio; impulso inicial por CIERRE ±0.5% / ±1% en los 30 minutos siguientes.',
        '- El pico del primer impulso se congela al cerrar un retroceso ≥50%. Las distancias de geometría se calculan en ESE cierre, usando solamente pasado.',
        '- Las ventanas posteriores empiezan en la vela SIGUIENTE al cierre de confirmación; horizontes 5/10/15/30m desde ese instante.',
        '- La carrera `SECOND_BREAKOUT / BASELINE_CROSSED` solo aplica si al confirmar retroceso todavía está del lado del primer impulso del precio t5.',
        '- Carrera simétrica ±0.25%, ±0.50% y ±1.00% por CIERRE desde el cierre del retroceso. MFE/MAE usan extremos OHLC sin inferir orden intravela.',
        '- Reingreso futuro al swing se evalúa solo si la vela del retroceso TODAVÍA estaba fuera de la zona ±0.10%.',
        '- Las métricas de `room` son distancias geométricas descriptivas, NO márgenes de stop ni resultados de entrada.',
        '- Pares estrictos: misma fecha y lado, características al minuto 5, sin solapamiento cercano; se requiere cobertura en ambos para cada comparación.',
        '', '## Archivos',
        '- `geometry_events.csv`: anatomía por evento, confirmaciones y distancias conocidas al retroceso.',
        '- `post_pullback_windows.csv`: desenlaces posteriores por evento/umbral/5,10,15,30m, cobertura y MFE/MAE descriptivos.',
        '- `geometry_summary.csv`: resultados por estado, lado y dirección del impulso.',
        '- `focus_hypotheses_summary.csv`: comparativo descriptivo de FAILED LONG/BREAKOUT y FAILED SHORT/REVERSAL en 10 y 30m (con N de cobertura).',
        '- `paired_strict.csv` / `paired_summary.csv`: comparaciones emparejadas con cobertura simétrica.',
        '- `flow_geometry_asof.csv`: estratos de flow causal solo cuando existe historial válido.',
        '- `daily_geometry_summary.csv`: frecuencia y excursiones por día UTC, sin declarar OOS.',
        '', '## Limitaciones']+[f'- {c}' for c in report['caveats']]
    (outdir/'ANALYSIS.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true')
    g.add_argument('--run',action='store_true')
    a=ap.parse_args()
    project=a.project.resolve()
    if a.check:
        events=v2.read_all_first_touches(project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3',
                    project/'reports/reaction_lab/first_touch_paths_v2.sqlite3')
        print(json.dumps(dict(check='OK_NO_WRITE',first_touches=len(events),
                  failed=sum(e['status']=='TOUCH_FAILED' for e in events),
                  flow_archive_exists=(project/v2.ARCHIVE_REL).is_file()),indent=2))
    else:print(json.dumps(run(project),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
