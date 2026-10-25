#!/usr/bin/env python3
"""FAILED Early Behavior V5 — causal landmarks after V4's second-break close.

Research ONLY. No PnL/fills, no Redis commands, no journal/path writes.
Only report files under reports/reaction_lab/failed_early_behavior_v5 are changed.

Semantics:
* t0 = SECOND_BREAKOUT's fully closed 1m candle (recognized by V4).
* t1/2/3/5 = the closed 1m bar that far AFTER second breakout.
* Features use bars <= landmark; forward labels use closes strictly AFTER it.
* First CLOSE races +/- .25/.50/1% are from the landmark close, NOT t0.
* MFE/MAE use H/L and make NO claim about their intrabar order.
* 'No hit' is only recorded with a complete forward window.
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import reaction_lab_failed_behavior_v1 as v1
import reaction_lab_failed_behavior_v2 as v2
import reaction_lab_failed_geometry_v3 as v3
import reaction_lab_failed_second_break_v4 as v4

OUT_REL = 'reports/reaction_lab/failed_early_behavior_v5'
LANDMARKS = (1,2,3,5)
FUTURES = (5,10,15,30)
THRESHOLDS = (.5,1.0)
RACES = (.25,.5,1.0)
TOKENS = {.25:'0p25',.5:'0p5',1.0:'1p0'}


def early_landmark(ev, episode, minute):
    """Features available ONLY at the landmark close after second break."""
    if not episode or episode.get('second_status') != 'SECOND_BREAKOUT':
        return None
    s = int(episode['second_min'])
    if minute not in LANDMARKS or len(ev['bars']) < s+minute:
        return None
    bars=ev['bars']; prefix=bars[s:s+minute]
    side=ev['side']; direction=int(episode['first_direction_sign'])
    price_sign=v3.is_forward_side(side,direction)
    anchor=episode['second_close_price'];last=prefix[-1]
    now=last[4]
    baseline=episode['baseline_m5_price']
    progress=lambda p:direction*v2.side_move(p,baseline,side)
    pk=episode['frozen_peak_progress_pct']
    current_progress=progress(now)
    prev=[progress(b[4]) for b in prefix]
    recrossed=any(x<=pk for x in prev)
    first_recross_known=next((i for i,z in enumerate(prev,1) if z<=pk),None)
    above=current_progress>pk
    move_from_second=v3.directed_pct(now,anchor,side,direction)
    # Distinguish first failure of the peak, subsequent reclaim, a new push,
    # and ongoing deterioration while still beyond the peak.
    if not above:state='LOST_PEAK'
    elif recrossed:state='RECLAIMED_PEAK'
    elif move_from_second>=.25:state='EXTENDED_0P25'
    elif move_from_second<=-.25:state='FADED_0P25'
    else:state='STABLE_ABOVE'
    extrema=[v3.edge_prices(bar,price_sign) for bar in prefix]
    high_fav=max([0]+[v3.directed_pct(fav,anchor,side,direction) for fav,_ in extrema])
    high_adv=max([0]+[-v3.directed_pct(adv,anchor,side,direction) for _,adv in extrema])
    swings=v1.positive(ev['meta'].get('swing_price'))
    size=last[2]-last[3]
    close_loc=((now-last[3])/size if price_sign==1 else (last[2]-now)/size) if size>0 else None
    last_body=v3.directed_pct(last[4],last[1],side,direction)
    # This is descriptive at t_landmark, not a momentum forecast.
    increases=sum(v3.directed_pct(x[4],x[1],side,direction)>0 for x in prefix)
    out=dict(touch_key=ev['key'],symbol=ev['symbol'],cohort_type=episode['cohort_type'],
       original_side=side,impulse_direction=episode['impulse_direction'],
       observation_type=ev['observation'],date_utc=episode['date_utc'],
       threshold_pct=episode['threshold_pct'],second_min=s,
       second_break_price=anchor,known_min=s+minute,
       early_minute=minute,landmark_close_price=now,
       early_state=state,already_recrossed_peak=recrossed,
       first_recross_known_min=first_recross_known,
       still_beyond_peak=above,prior_peak_progress_pct=pk,
       current_progress_from_m5_pct=current_progress,
       move_since_second_pct=move_from_second,
       early_mfe_wick_pct=high_fav,early_mae_wick_pct=high_adv,
       distance_to_peak_pct=v3.directed_pct(now,episode['frozen_peak_price'],side,direction),
       distance_to_peak_return_pct=v3.directed_pct(episode['frozen_peak_price'],now,side,direction),
       distance_to_m5_pct=v3.directed_pct(baseline,now,side,-direction),
       distance_to_swing_break_pct=v1.break_distance(now,swings,side) if swings else None,
       early_above_peak_closes=sum(z>pk for z in prev),
       early_above_peak_fraction=sum(z>pk for z in prev)/minute,
       early_green_bars=increases,
       last_bar_body_direction_pct=last_body,
       two_close_momentum_pct=(v3.directed_pct(now,prefix[-2][4],side,direction) if minute>=2 else None),
       last_bar_range_pct=(size/now*100 if now>0 else None),
       last_bar_close_location_favorable=close_loc,
       flow_asof_available=episode.get('flow_asof_available',False),
       return_rank_4h_pct=episode.get('return_rank_4h_pct'),
       volume_rank_4h_pct=episode.get('volume_rank_4h_pct'),
       relative_volume_4h=episode.get('relative_volume_4h'),
       flow_group=episode.get('flow_group'))
    return out


def forward_window(ev,episode,known,horizon):
    """Outcomes are strictly AFTER the observed t1/t2/t3/t5 close."""
    if known is None:return None
    s=known['known_min'];bars=ev['bars']
    available=max(0,len(bars)-s)
    full=available>=horizon
    forward=bars[s:s+min(horizon,available)]
    direction=int(episode['first_direction_sign'])
    side=ev['side']; price_sign=v3.is_forward_side(side,direction)
    landmark=known['landmark_close_price']
    moves=[v3.directed_pct(b[4],landmark,side,direction) for b in forward]
    extreme=[v3.edge_prices(b,price_sign) for b in forward]
    pk=episode['frozen_peak_progress_pct'];baseline=episode['baseline_m5_price']
    post_progress=[direction*v2.side_move(b[4],baseline,side) for b in forward]
    at_risk=bool(known['still_beyond_peak'])
    recross=next((i for i,z in enumerate(post_progress,1) if z<=pk),None)
    retouch_m5=next((i for i,z in enumerate(post_progress,1) if z<=0),None)
    out={k:known[k] for k in ('touch_key','symbol','cohort_type','original_side','impulse_direction',
        'observation_type','date_utc','threshold_pct','second_min','known_min','early_minute',
        'early_state','already_recrossed_peak','still_beyond_peak','move_since_second_pct',
        'distance_to_peak_return_pct','distance_to_m5_pct','return_rank_4h_pct',
        'volume_rank_4h_pct','flow_group','flow_asof_available')}
    out.update(forward_horizon=horizon,available_forward_bars=available,
       full_coverage=full,censored=not full,eligible_future_recross=at_risk,
       # Close and extremes have different semantics: never infer sequence from H/L.
       future_mfe_wick_pct=max([0]+[v3.directed_pct(f,landmark,side,direction) for f,_ in extreme]) if full else None,
       future_mae_wick_pct=max([0]+[-v3.directed_pct(a,landmark,side,direction) for _,a in extreme]) if full else None,
       future_close_change_pct=moves[-1] if full and moves else None,
       future_fraction_beyond_peak=sum(x>pk for x in post_progress)/horizon if full else None,
       future_peak_recross_status=('RE_CROSSED' if recross else 'HELD') if full and at_risk else ('ALREADY_LOST' if not at_risk else 'CENSORED'),
       future_peak_recross_delay_min=recross if full and at_risk else None,
       future_base_touch_status=('TOUCHED' if retouch_m5 else 'NOT_TOUCHED') if full else 'CENSORED',
       future_base_touch_delay_min=retouch_m5 if full else None,
       future_close_beyond_peak=(post_progress[-1]>pk if full and post_progress else None))
    for threshold in RACES:
        label,delay,both=v3.race_on_closes(moves,threshold)
        tag=TOKENS[threshold]
        out[f'first_{tag}']=label if full else 'CENSORED'
        out[f'first_{tag}_delay']=delay if full else None
        out[f'both_{tag}']=both if full else None
    return out


def median(items):return v3.median(x for x in items if x is not None)


def summary(rows,fields):
    buckets=defaultdict(list)
    for r in rows:buckets[tuple(r[f] for f in fields)].append(r)
    output=[]
    for key,group in sorted(buckets.items(),key=lambda x:str(x[0])):
        ready=[x for x in group if x['full_coverage']]
        n=len(ready)
        pct=lambda cond:round(100*sum(bool(cond(x)) for x in ready)/n,4) if n else None
        med=lambda col:median(x[col] for x in ready)
        states=defaultdict(int)
        for x in group:states[x['early_state']]+=1
        output.append(dict(zip(fields,key)) | dict(
            N=len(group),N_full=n,N_censored=len(group)-n,
            continue_first_0p25_pct=pct(lambda x:x['first_0p25']=='CONTINUE'),
            reverse_first_0p25_pct=pct(lambda x:x['first_0p25']=='REVERSE'),
            continue_first_0p5_pct=pct(lambda x:x['first_0p5']=='CONTINUE'),
            reverse_first_0p5_pct=pct(lambda x:x['first_0p5']=='REVERSE'),
            neither_0p5_pct=pct(lambda x:x['first_0p5']=='NEITHER'),
            continue_first_1p0_pct=pct(lambda x:x['first_1p0']=='CONTINUE'),
            reverse_first_1p0_pct=pct(lambda x:x['first_1p0']=='REVERSE'),
            median_first_half_delay=med('first_0p5_delay'),
            peak_recross_risk_n=sum(x['eligible_future_recross'] for x in ready),
            peak_recross_future_n=sum(x['future_peak_recross_status']=='RE_CROSSED' for x in ready),
            median_future_mfe_pct=med('future_mfe_wick_pct'),
            median_future_mae_pct=med('future_mae_wick_pct'),
            median_future_close_pct=med('future_close_change_pct'),
            median_future_fraction_beyond_peak=med('future_fraction_beyond_peak')))
    return output


def paired_landmarks(matches,known,windows):
    """Require same impulse direction AND future completeness in BOTH legs.

    Selected after secondary breakout; this is a heavily selected descriptive
    comparison (not matched causal treatment, not general market control).
    """
    ks={(r['touch_key'],r['threshold_pct'],r['early_minute']):r for r in known}
    ws={(r['touch_key'],r['threshold_pct'],r['early_minute'],r['forward_horizon']):r for r in windows}
    out=[]
    for match in matches:
        for th in THRESHOLDS:
            for minute in LANDMARKS:
                f=ks.get((match['failed_key'],th,minute));c=ks.get((match['control_key'],th,minute))
                if not f or not c:continue
                for h in FUTURES:
                    a=ws.get((match['failed_key'],th,minute,h));b=ws.get((match['control_key'],th,minute,h))
                    complete=bool(a and b and a['full_coverage'] and b['full_coverage'])
                    same_impulse=bool(f['impulse_direction']==c['impulse_direction'])
                    same_state=bool(f['early_state']==c['early_state'])
                    out.append(dict(failed_key=f['touch_key'],control_key=c['touch_key'],threshold_pct=th,
                        landmark=minute,forward_horizon=h,original_side=f['original_side'],
                        impulse_direction=f['impulse_direction'],
                        both_second_break=True,both_full=complete,both_same_impulse=same_impulse,
                        both_same_early_state=bool(same_impulse and same_state),
                        both_still_beyond_peak=bool(f['still_beyond_peak'] and c['still_beyond_peak']),
                        failed_early_state=f['early_state'],control_early_state=c['early_state'],
                        failed_race_0p5=a['first_0p5'] if complete else 'INELIGIBLE',
                        control_race_0p5=b['first_0p5'] if complete else 'INELIGIBLE'))
    return out


def run(project):
    project=Path(project)
    src=project/'reports/reaction_lab'
    events=v2.read_all_first_touches(src/'scanner_first_touches_v1.sqlite3',src/'first_touch_paths_v2.sqlite3')
    failed=[e for e in events if e['status']=='TOUCH_FAILED']
    control=[e for e in events if e['status'] in ('REACTION','TOUCH_INDECISIVE')]
    seen_f=[v2.observation(e,5) for e in failed];seen_c=[v2.observation(e,5) for e in control]
    matches=v2.nearest_controls([x for x in seen_f if x],[x for x in seen_c if x])
    keys={p['control_key'] for p in matches}
    selected=[*failed,*(e for e in control if e['key'] in keys)]
    flow,flow_status=v2.read_flow_asof(project,selected)
    episodes=[];landmarks=[];windows=[];curves=[]
    for ev in selected:
        for threshold in THRESHOLDS:
            ep=v4.recognize(ev,threshold)
            if not ep:continue
            ep['flow_asof_available']=ev['key'] in flow
            if ev['key'] in flow:
                f=flow[ev['key']]
                ep.update(return_rank_4h_pct=f.get('return_rank_4h_pct'),
                    volume_rank_4h_pct=f.get('volume_rank_4h_pct'),
                    relative_volume_4h=f.get('relative_volume_4h'),flow_group=f.get('flow_group'))
            episodes.append(ep)
            if ep['second_status']!='SECOND_BREAKOUT':continue
            for minute in LANDMARKS:
                obs=early_landmark(ev,ep,minute)
                if obs is None:continue
                landmarks.append(obs)
                for h in FUTURES:
                    windows.append(forward_window(ev,ep,obs,h))
                # For graphics: normalized closes from landmark, coverage explicit.
                s=obs['known_min'];anchor=obs['landmark_close_price']
                for j,bar in enumerate(ev['bars'][s:s+30],1):
                    curves.append(dict(touch_key=ev['key'],symbol=ev['symbol'],
                      date_utc=obs['date_utc'],cohort_type=obs['cohort_type'],
                      observation_type=obs['observation_type'],
                      original_side=obs['original_side'],impulse_direction=obs['impulse_direction'],
                      threshold_pct=threshold,early_minute=minute,early_state=obs['early_state'],
                      future_minute=j,forward_change_pct=v3.directed_pct(bar[4],anchor,ev['side'],int(ep['first_direction_sign'])),
                      completed_30m=len(ev['bars'])>=s+30))
    out=project/OUT_REL
    summ=summary(windows,('cohort_type','original_side','impulse_direction','threshold_pct',
                          'early_minute','early_state','forward_horizon'))
    focus=summary([r for r in windows if (r['original_side']=='LONG' and r['impulse_direction']=='BREAKOUT') or
             (r['original_side']=='SHORT' and r['impulse_direction']=='REVERSAL')],
             ('cohort_type','original_side','impulse_direction','threshold_pct',
              'early_minute','forward_horizon'))
    daily=summary(windows,('date_utc','cohort_type','original_side','impulse_direction','threshold_pct',
                          'early_minute','forward_horizon'))
    paired=paired_landmarks(matches,landmarks,windows)
    v2.rows_to_csv(out/'second_break_episodes.csv',episodes)
    v2.rows_to_csv(out/'early_landmarks.csv',landmarks)
    v2.rows_to_csv(out/'forward_windows.csv',windows)
    v2.rows_to_csv(out/'early_behavior_summary.csv',summ)
    v2.rows_to_csv(out/'focus_hypotheses_summary.csv',focus)
    v2.rows_to_csv(out/'daily_consistency.csv',daily)
    v2.rows_to_csv(out/'strict_paired_early.csv',paired,
        columns=list(paired[0]) if paired else ['failed_key','control_key','threshold_pct','landmark',
           'forward_horizon','both_full','both_same_impulse','both_same_early_state'])
    v2.rows_to_csv(out/'future_trajectories.csv',curves)
    counts=lambda typ:len({x['touch_key'] for x in episodes if x['cohort_type']==typ and
                                           x['threshold_pct']==.5 and x['second_status']=='SECOND_BREAKOUT'})
    flow_count=sum(ev['key'] in flow for ev in failed)
    report=dict(generated_utc=datetime.now(timezone.utc).isoformat(),
        failed_total=len(failed),controls_selected=len(keys),matches=len(matches),
        second_break_failed_0p5=counts('FAILED'),second_break_control_0p5=counts('MATCHED_NONFAILED'),
        failed_landmarks_3m_0p5=len([x for x in landmarks if x['cohort_type']=='FAILED' and
                                                      x['threshold_pct']==.5 and x['early_minute']==3]),
        windows_complete_3m_15m_0p5=len([x for x in windows if x['cohort_type']=='FAILED' and
                              x['threshold_pct']==.5 and x['early_minute']==3 and
                              x['forward_horizon']==15 and x['full_coverage']]),
        asof_flow_archive_status=flow_status,failed_with_flow_asof=flow_count,
        strict_pair_both_full_0p5_t3_h15=sum(x['both_full'] and x['threshold_pct']==.5 and
            x['landmark']==3 and x['forward_horizon']==15 for x in paired),
        outdir=str(out),notes=[
            'Second break uses V4 close-confirmed causal definition; no retrospective selection of a profitable entry.',
            't0 is the second-break CLOSE; t1/t2/t3/t5 are fully known 1m CLOSE landmarks.',
            'Every forward movement is normalized to the landmark close and observed from NEXT candle.',
            'Already-recrossed peak is not treated as a future recross candidate.',
            'CENSORED outcomes are never treated as no movement.',
            'Wick MFE and MAE are not ordered intrabar and are not executable returns.',
            'First-touch matched controls are not random market bars; selecting both secondary breaks induces bias.',
            'Outcomes are correlated by day/symbol; multi-comparison exploration is NOT OOS verification.',
            'Volume features NOT available in OHLC-only stored 1m paths; archived flow as-of may be sparse.',
            'No PnL, cost model, fills, strategy thresholds, or LIVE service changes.'])
    out.mkdir(parents=True,exist_ok=True)
    (out/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    analysis=(
        '# FAILED Early Behavior V5 — ¿continúa o se agota el segundo quiebre?\n\n'
        '**Solo investigación descriptiva. No PnL ni señales de ejecución.**\n\n'
        f'- FAILED en journal: **{len(failed)}**.\n'
        f'- FAILED con segundo quiebre ±0,5%: **{report["second_break_failed_0p5"]}**.\n'
        f'- Con minuto 3 observado: **{report["failed_landmarks_3m_0p5"]}**.\n'
        f'- Con 15m futuros completos desde minuto 3: **{report["windows_complete_3m_15m_0p5"]}**.\n'
        f'- Pares completos en t3/h15: **{report["strict_pair_both_full_0p5_t3_h15"]}** (muestra seleccionada).\n'
        f'- FAILED con Flow 4h as-of en la hora original: **{flow_count}**.\n\n'
        '## Definiciones\n'
        '- Estado temprano: se conoce al CIERRE t1/t2/t3/t5 del segundo quiebre; nunca usa velas posteriores.\n'
        '- Cierres futuros +/−0,25/0,5/1% se observan desde el CIERRE del landmark, sin anticipar velas.\n'
        '- Peak recross futuro es evaluable SOLO si todavía estaba encima del pico al landmark.\n'
        '- OHLC extremo permite medir MFE/MAE, NO el orden ni la capacidad real de ejecución.\n'
        '- Contexto Market Flow únicamente si se archivó antes del evento original.\n\n'
        '## Reportes\n'
        '- early_landmarks.csv: observaciones causales por minuto y evento.\n'
        '- forward_windows.csv: primeras excursiones, MFE/MAE y retorno con cobertura y censura.\n'
        '- early_behavior_summary.csv: cohortes por dirección y estado temprano.\n'
        '- focus_hypotheses_summary.csv: FAILED LONG/BREAKOUT y SHORT/REVERSAL frente a control.\n'
        '- strict_paired_early.csv: pares con estado/cobertura verificables; no evidencia causal.\n'
        '- future_trajectories.csv: trayectoria futura firmada para curvas agregadas.\n'
        '- second_break_episodes.csv: totalidad de episodios, incluidos sin segundo quiebre.\n'
        '- daily_consistency.csv: resultados por fecha UTC.\n')
    (out/'ANALYSIS.md').write_text(analysis,encoding='utf-8')
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    grp=ap.add_mutually_exclusive_group(required=True)
    grp.add_argument('--check',action='store_true')
    grp.add_argument('--run',action='store_true')
    args=ap.parse_args()
    project=args.project.resolve()
    if args.check:
        events=v2.read_all_first_touches(project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3',
                                          project/'reports/reaction_lab/first_touch_paths_v2.sqlite3')
        print(json.dumps({'check':'OK_READ_ONLY','first_touches':len(events),
          'failed':sum(e['status']=='TOUCH_FAILED' for e in events)},indent=2))
    else:
        print(json.dumps(run(project),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
