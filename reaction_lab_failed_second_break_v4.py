#!/usr/bin/env python3
"""V4: close-confirmed continuation AFTER the second breakout; no PnL/fills.

Uses V2 and V3 phase definitions. Only reads historical journals and 1m paths.
Writes separate CSV summaries; will never alter market-data, LIVE, or journal DBs.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import reaction_lab_failed_behavior_v1 as v1
import reaction_lab_failed_behavior_v2 as v2
import reaction_lab_failed_geometry_v3 as v3

OUT_REL='reports/reaction_lab/failed_second_break_v4'
THRESHOLDS=(.5, 1.0)
HORIZONS=(5,10,15,30)
FOLLOW_THRESHOLDS=(.25,.5,1.0)


def recognize(ev, threshold=.5):
    """All geometry at second break known by that 1m CLOSE, no hindsight."""
    a=v3.anatomical_event(ev,threshold=threshold)
    if a is None:
        return None
    r={k:a.get(k) for k in ('touch_key','symbol','cohort_type','original_side','observation_type',
         'date_utc','threshold_pct','impulse_direction','impulse_min','peak_min','pullback_min',
         'geometry_known_at_pb','risk_set_future_competition','minutes_impulse_to_pullback',
         'pb_retrace_fraction_pct','room_to_frozen_peak_pct','room_to_m5_baseline_pct',
         'initial_penetration_pct','first_5_outside','early_signed_5m_pct')}
    r.update(second_status=a.get('source_status','NO_PULLBACK'),second_min=None,second_delay_from_pb=None,
             second_close_price=None,second_progress_from_m5_pct=None,
             second_overshoot_frozen_peak_pct=None,second_minutes_from_fail=None,
             second_distance_to_swing_pct=None,observed_post_pb_closes=None,
             full_second_watch=False,flow_asof_available=False)
    if not a.get('geometry_known_at_pb'):
        return r
    if not a['risk_set_future_competition']:
        r['second_status']='BASELINE_ALREADY_AT_PB'
        return r
    pb=int(a['pullback_min'])
    available=max(0,len(ev['bars'])-pb)
    length=min(30,available)
    label,delay,_=v3.first_second_event(ev,pb,length,a['baseline_m5_price'],
                                        a['impulse_peak_pct'],a['first_direction_sign'])
    full=available>=30
    if label=='NEITHER' and not full:label='CENSORED_SECOND_WATCH'
    r.update(second_status=label,second_delay_from_pb=delay,
             observed_post_pb_closes=length,full_second_watch=full)
    if label!='SECOND_BREAKOUT':return r
    minute=pb+delay
    close=ev['bars'][minute-1][4]
    progress=a['first_direction_sign']*v2.side_move(close,a['baseline_m5_price'],ev['side'])
    swing=v1.positive(ev['meta'].get('swing_price'))
    r.update(second_min=minute,second_close_price=close,
             second_progress_from_m5_pct=progress,
             second_overshoot_frozen_peak_pct=progress-a['impulse_peak_pct'],
             second_minutes_from_fail=minute,
             second_distance_to_swing_pct=(v1.break_distance(close,swing,ev['side']) if swing else None),
             frozen_peak_price=a['frozen_peak_price'],
             frozen_peak_progress_pct=a['impulse_peak_pct'],
             pb_close_price=a['pb_close_price'],
             pb_progress_from_m5_pct=a['pullback_progress_from_m5_pct'],
             baseline_m5_price=a['baseline_m5_price'],
             first_direction_sign=a['first_direction_sign'])
    return r


def post_second(ev, episode, horizon):
    """T+1 onward; CLOSE for ordering, OHLC for unordered excursions."""
    if episode is None or episode['second_status']!='SECOND_BREAKOUT':return None
    s=int(episode['second_min']); bars=ev['bars']
    available=max(0,len(bars)-s)
    full=available>=horizon
    tail=bars[s:s+min(horizon,available)]
    close=episode['second_close_price']
    dr=episode['first_direction_sign']; side=ev['side']
    direction=v3.is_forward_side(side,dr)
    signed_closes=[v3.directed_pct(b[4],close,side,dr) for b in tail]
    fav=[];adv=[];peak_progress=[]
    for b in tail:
        favorable,adverse=v3.edge_prices(b,direction)
        fav.append(v3.directed_pct(favorable,close,side,dr))
        adv.append(v3.directed_pct(adverse,close,side,dr))
        peak_progress.append(dr*v2.side_move(b[4],episode['baseline_m5_price'],side))
    pk=episode['frozen_peak_progress_pct']
    pb_progress=episode['pb_progress_from_m5_pct']
    def first(pred):
        return next((i for i,v in enumerate(peak_progress,1) if pred(v)),None)
    recross=first(lambda z:z<=pk)
    pb_cross=first(lambda z:z<=pb_progress)
    base_cross=first(lambda z:z<=0)
    above=[v>pk+v3.BREAK_MARGIN for v in peak_progress]
    def max_run(seq):
        cur=mx=0
        for yes in seq:
            cur=(cur+1) if yes else 0
            mx=max(cur,mx)
        return mx
    d=dict(touch_key=ev['key'],symbol=ev['symbol'],original_side=side,
           cohort_type=episode['cohort_type'],observation_type=ev['observation'],
           date_utc=episode['date_utc'],impulse_direction=episode['impulse_direction'],
           threshold_pct=episode['threshold_pct'],post_second_horizon=horizon,
           second_min=s,second_delay_from_pb=episode['second_delay_from_pb'],
           full_coverage=full,censored=not full,available_future_bars=available,
           second_overshoot_pct=episode['second_overshoot_frozen_peak_pct'],
           second_distance_to_swing_pct=episode['second_distance_to_swing_pct'],
           initial_penetration_pct=episode['initial_penetration_pct'],
           early_signed_5m_pct=episode['early_signed_5m_pct'],
           flow_asof_available=episode.get('flow_asof_available',False),
           # These are only final window results if coverage is complete.
           post_second_mfe_pct=max([0]+fav) if full else None,
           post_second_mae_pct=max([0]+[-x for x in adv]) if full else None,
           post_second_close_change_pct=signed_closes[-1] if full and signed_closes else None,
           first_recross_peak_min=recross if full else None,
           first_retouch_pb_min=pb_cross if full else None,
           first_retouch_m5_min=base_cross if full else None,
           recross_peak_status=('YES' if recross else 'NO') if full else 'CENSORED',
           retouch_pb_status=('YES' if pb_cross else 'NO') if full else 'CENSORED',
           retouch_m5_status=('YES' if base_cross else 'NO') if full else 'CENSORED',
           fraction_closes_beyond_peak=sum(above)/horizon if full else None,
           longest_consecutive_beyond_peak=max_run(above) if full else None,
           first_close_under_peak_min=recross if full else None)
    for th in FOLLOW_THRESHOLDS:
        token={.25:'0p25',.5:'0p5',1.0:'1p0'}[th]
        k,t,both=v3.race_on_closes(signed_closes,th)
        d[f'first_{token}']=k if full else 'CENSORED'
        d[f'first_{token}_min']=t if full else None
        d[f'both_{token}']=both if full else None
    return d


def summarize(rows,keys):
    buckets=defaultdict(list)
    for r in rows:buckets[tuple(r[k] for k in keys)].append(r)
    out=[]
    for g,x in sorted(buckets.items(),key=lambda kv:str(kv[0])):
        full=[q for q in x if q['full_coverage']]
        n=len(full)
        frac=lambda fn:100*sum(fn(q) for q in full)/n if n else None
        med=lambda field:v3.median(q[field] for q in full)
        out.append(dict(zip(keys,g)) | dict(
            N=len(x),N_full=n,N_censored=len(x)-n,
            first_continue_0p25_pct=frac(lambda q:q['first_0p25']=='CONTINUE'),
            first_reverse_0p25_pct=frac(lambda q:q['first_0p25']=='REVERSE'),
            first_continue_0p5_pct=frac(lambda q:q['first_0p5']=='CONTINUE'),
            first_reverse_0p5_pct=frac(lambda q:q['first_0p5']=='REVERSE'),
            first_none_0p5_pct=frac(lambda q:q['first_0p5']=='NEITHER'),
            first_continue_1p0_pct=frac(lambda q:q['first_1p0']=='CONTINUE'),
            peak_recross_pct=frac(lambda q:q['recross_peak_status']=='YES'),
            pb_retouch_pct=frac(lambda q:q['retouch_pb_status']=='YES'),
            baseline_retouch_pct=frac(lambda q:q['retouch_m5_status']=='YES'),
            median_recross_min=med('first_recross_peak_min'),
            median_first_half_min=med('first_0p5_min'),
            median_mfe_pct=med('post_second_mfe_pct'),
            median_mae_pct=med('post_second_mae_pct'),
            median_close_pct=med('post_second_close_change_pct'),
            median_fraction_beyond_peak=med('fraction_closes_beyond_peak'),
            median_consecutive_closes_beyond_peak=med('longest_consecutive_beyond_peak')))
    return out


def paired_post_second(matches,episodes,windows,horizon=15,threshold=.5):
    """Comparison subset, BOTH completed second break and future window.

    Heavily selected on downstream phase; describe only, never causal/OOS.
    """
    epi={(e['touch_key'],e['threshold_pct']):e for e in episodes}
    w={(e['touch_key'],e['threshold_pct'],e['post_second_horizon']):e for e in windows}
    rows=[]
    for m in matches:
        f=epi.get((m['failed_key'],threshold));c=epi.get((m['control_key'],threshold))
        if not f or not c:continue
        fw=w.get((m['failed_key'],threshold,horizon));cw=w.get((m['control_key'],threshold,horizon))
        both_second=(f['second_status']=='SECOND_BREAKOUT' and c['second_status']=='SECOND_BREAKOUT')
        complete=bool(both_second and fw and cw and fw['full_coverage'] and cw['full_coverage'])
        rows.append(dict(failed_key=m['failed_key'],control_key=m['control_key'],
           side=f['original_side'],failed_impulse=f['impulse_direction'],
           control_impulse=c['impulse_direction'],threshold_pct=threshold,horizon=horizon,
           both_second=both_second,both_full=complete,
           both_same_impulse=bool(complete and f['impulse_direction']==c['impulse_direction']),
           failed_first_half=(fw['first_0p5'] if complete else 'INELIGIBLE'),
           control_first_half=(cw['first_0p5'] if complete else 'INELIGIBLE'),
           failed_recross=(fw['recross_peak_status'] if complete else 'INELIGIBLE'),
           control_recross=(cw['recross_peak_status'] if complete else 'INELIGIBLE')))
    return rows


def run(project):
    project=Path(project)
    src=project/'reports/reaction_lab'
    events=v2.read_all_first_touches(src/'scanner_first_touches_v1.sqlite3',src/'first_touch_paths_v2.sqlite3')
    failed=[e for e in events if e['status']=='TOUCH_FAILED']
    control=[e for e in events if e['status'] in ('REACTION','TOUCH_INDECISIVE')]
    obs_f=[v2.observation(e,5) for e in failed];obs_c=[v2.observation(e,5) for e in control]
    pairs=v2.nearest_controls([x for x in obs_f if x],[x for x in obs_c if x])
    picked={p['control_key'] for p in pairs}
    selected=[*failed,*(e for e in control if e['key'] in picked)]
    # Archived flow is read-only and joined BEFORE original event; no future snapshots.
    asof,flow_status=v2.read_flow_asof(project,selected)
    episodes=[];windows=[]
    for e in selected:
        for th in THRESHOLDS:
            epi=recognize(e,th)
            if not epi:continue
            epi['flow_asof_available']=e['key'] in asof
            if e['key'] in asof:
                f=asof[e['key']]
                epi.update(flow_group=f['flow_group'],return_rank_4h_pct=f['return_rank_4h_pct'],
                           volume_rank_4h_pct=f['volume_rank_4h_pct'],relative_volume_4h=f['relative_volume_4h'])
            episodes.append(epi)
            if epi['second_status']=='SECOND_BREAKOUT':
                windows.extend(post_second(e,epi,h) for h in HORIZONS)
    out=project/OUT_REL
    grouped=summarize(windows,('cohort_type','original_side','impulse_direction','threshold_pct','post_second_horizon'))
    focus=[r for r in grouped if r['threshold_pct']==.5 and ((r['original_side']=='SHORT' and r['impulse_direction']=='REVERSAL') or (r['original_side']=='LONG' and r['impulse_direction']=='BREAKOUT'))]
    daily=summarize(windows,('date_utc','cohort_type','original_side','impulse_direction','threshold_pct','post_second_horizon'))
    paired=[]
    for th in THRESHOLDS:
        for h in HORIZONS:paired.extend(paired_post_second(pairs,episodes,windows,h,th))
    flow_rows=summarize([r for r in windows if r['flow_asof_available']],('original_side','impulse_direction','threshold_pct','post_second_horizon'))
    v2.rows_to_csv(out/'second_break_events.csv',episodes)
    v2.rows_to_csv(out/'post_second_windows.csv',windows)
    v2.rows_to_csv(out/'second_break_summary.csv',grouped)
    v2.rows_to_csv(out/'focus_hypotheses_summary.csv',focus)
    v2.rows_to_csv(out/'daily_second_summary.csv',daily)
    v2.rows_to_csv(out/'strict_paired_post_second.csv',paired)
    v2.rows_to_csv(out/'flow_second_asof.csv',flow_rows,
        columns=list(flow_rows[0]) if flow_rows else ['original_side','impulse_direction','threshold_pct','post_second_horizon','N','N_full'])
    report=dict(generated_at_utc=datetime.now(timezone.utc).isoformat(),failed=len(failed),
       control_selected=len(picked),matched_pairs=len(pairs),flow_archive_status=flow_status,
       failed_flow_asof=sum(e['key'] in asof for e in failed),
       failed_second_break_0p5=sum(e['cohort_type']=='FAILED' and e['threshold_pct']==.5 and e['second_status']=='SECOND_BREAKOUT' for e in episodes),
       failed_second_complete_15m_0p5=sum(r['cohort_type']=='FAILED' and r['threshold_pct']==.5 and r['post_second_horizon']==15 and r['full_coverage'] for r in windows),
       notes=['Second breakout is CONFIRMED on a 1m CLOSE; future starts at the NEXT 1m candle.',
              'Fixed V2/V3 definition: first ±0.5% or ±1% from t5, first ≥50% retrace; peak+0.05 pp before baseline.',
              'No PnL, fills, leverage, fees, entry optimization or ordering inference from OHLC wicks.',
              'Events with incomplete future bars censored; no-event and already-crossed-baseline distinguished.',
              'Controls are other matched first touches, not random market bars; conditioning on both reaching second break is selected and descriptive.',
              'Flow as-of never uses snapshots observed after the event. Snapshot history starts at installation.',
              'Multiple comparisons and correlated symbols; no OOS support yet.'],outdir=str(out))
    out.mkdir(parents=True,exist_ok=True)
    (out/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    (out/'ANALYSIS.md').write_text('# FAILED Second Break Lab V4 — continuación e invalidación\n\n'
      'Descriptivo, NO PnL ni señal operativa.\n\n'
      f'- FAILED: {len(failed)}; controles emparejados: {len(pairs)}.\n'
      f'- FAILED con segundo quiebre confirmado (±0,5%): {report["failed_second_break_0p5"]}.\n'
      f'- De esos, ventana posterior 15m completa: {report["failed_second_complete_15m_0p5"]}.\n'
      f'- Cobertura Market Flow causal en FAILED: {report["failed_flow_asof"]}.\n\n'
      '## Interpretación\n'
      '- `second_break_events.csv` incluye todos los FAILED y los controles seleccionados, incluso si no hubo segundo quiebre.\n'
      '- `post_second_windows.csv` comienza desde la vela posterior al segundo quiebre confirmado.\n'
      '- Continuación e invalidación por primer CIERRE ±0,25/0,50/1,00% desde el cierre del segundo quiebre.\n'
      '- `recross_peak` significa cierre de regreso al pico original o por debajo (en eje del primer impulso).\n'
      '- `retouch_pb` y `retouch_m5` son umbrales sucesivos, no stops simulados.\n'
      '- MFE/MAE intravela son magnitudes; no prueban cuál vino primero.\n'
      '- Pares requieren segundo quiebre en AMBOS y ventana futura completa en AMBOS, con selección a posteriori.\n'
      '- No extrapolar grupos de pocas observaciones ni comparar como causalidad.\n',encoding='utf8')
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--check',action='store_true')
    group.add_argument('--run',action='store_true')
    args=ap.parse_args()
    project=args.project.resolve()
    if args.check:
        all_ev=v2.read_all_first_touches(project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3',
                                          project/'reports/reaction_lab/first_touch_paths_v2.sqlite3')
        print(json.dumps({'check':'OK_READONLY','first_touches':len(all_ev),
            'failed':sum(e['status']=='TOUCH_FAILED' for e in all_ev)},indent=2))
    else:print(json.dumps(run(project),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
