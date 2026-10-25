#!/usr/bin/env python3
"""V6: frozen t1 pathway audit (not an optimizer; NO PnL or trading).

Reads first-touch journal and OHLC archive RO, reuses the unmodified V4/V5
phase detectors. Writes reports ONLY to the dedicated V6 reports directory.
Selected focus is FIXED: first impulse 0.5%; original LONG + BREAKOUT;
second close-confirmed break; inspect its first subsequent closed candle (t1).
Two exploratory pathways: EXTENDED_0P25, LOST_PEAK. Others remain in the audit.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import reaction_lab_failed_behavior_v2 as v2
import reaction_lab_failed_geometry_v3 as v3
import reaction_lab_failed_second_break_v4 as v4
import reaction_lab_failed_early_behavior_v5 as v5

OUT_REL = 'reports/reaction_lab/failed_path_audit_v6'
FREEZE_UTC = '2026-10-25T20:51:15+00:00'  # first historical V5 run, not adjustable for fit
FREEZE_MS = int(datetime.fromisoformat(FREEZE_UTC).timestamp()*1000)
THRESHOLD = .5
LANDMARK = 1
HORIZONS = (5,15,30)
FOLLOW_HORIZONS = (5,15)
ADVANCE_MARGIN_PP = .05  # same margin in progress-percentage-points as V4


def numerical(x):
    try:
        y = float(x)
        return y if math.isfinite(y) else None
    except (ValueError, TypeError):
        return None


def first_index(values, fn):
    return next((i for i, x in enumerate(values, 1) if fn(x)), None)


def signed_progress(close, episode, side):
    return int(episode['first_direction_sign'])*v2.side_move(close,episode['baseline_m5_price'],side)


def price_at_progress(episode, side, progress):
    sign=v3.is_forward_side(side,int(episode['first_direction_sign']))
    return episode['baseline_m5_price']*(1+sign*progress/100)


def cohort_era(ev, obs, future_horizon=30):
    """Strict, conservative chronological split; no OOS claims for backfills."""
    cutoff=FREEZE_MS
    # Event first post-retest bar opens at ts+1m, known_min-th bar closes at
    # ts+(known_min+1)m. Last requested future bar closes after horizon minutes.
    end=ev['ts']+(obs['known_min']+future_horizon+1)*60000
    if ev['ts']>cutoff and ev['seen_ms']>cutoff and ev['observation']=='LIVE_OBSERVED':
        return 'FORWARD_LIVE'
    if ev['ts']<=cutoff and ev['seen_ms']<=cutoff and end<=cutoff:
        return 'HISTORICAL_EXPLORATORY'
    return 'BACKFILL_OR_BOUNDARY'


def classify(obs):
    x=obs['early_state']
    return ('A_ACCELERATED' if x=='EXTENDED_0P25'
            else 'B_LOST_PEAK' if x=='LOST_PEAK'
            else 'OTHER_'+x)


def measure_window(ev, episode, obs, horizon):
    """Future races exclusively on CLOSE; reference next OPEN (not a fill)."""
    if episode['second_status']!='SECOND_BREAKOUT' or obs['early_minute']!=1:
        raise ValueError('Expected t1 second-break event')
    bars=ev['bars'];s=int(obs['known_min'])
    future=bars[s:s+horizon]
    full=len(future)==horizon
    state=classify(obs)
    pk=float(episode['frozen_peak_progress_pct'])
    # A tests a fresh low/high beyond the strongest known CLOSE at t1.
    # B tests a TRUE reclaim of the original second break, NOT merely
    # a signed 0.5% move from an already-invalidated landmark.
    past_progress=[signed_progress(b[4],episode,ev['side']) for b in bars[int(episode['second_min'])-1:s]]
    assert len(past_progress)==2  # t0 and t1 only
    observed_extreme=max(past_progress)
    fresh_threshold=(observed_extreme+ADVANCE_MARGIN_PP if state=='A_ACCELERATED'
                    else pk+ADVANCE_MARGIN_PP)
    progress=[signed_progress(b[4],episode,ev['side']) for b in future]
    fresh=first_index(progress,lambda x:x>=fresh_threshold-1e-9)
    invalid=first_index(progress,lambda x:x<=pk+1e-9)
    baseline=first_index(progress,lambda x:x<=0+1e-9)
    # B: compare true second-break reentry against reaching m5 baseline.
    baseline_already_at_t1=(state=='B_LOST_PEAK' and signed_progress(obs['landmark_close_price'],episode,ev['side'])<=0)
    competing_delay=baseline if state=='B_LOST_PEAK' else invalid
    if baseline_already_at_t1:competing='BASELINE_ALREADY_AT_T1'
    elif fresh is not None and (competing_delay is None or fresh<competing_delay):
        competing='FRESH_BREAK_FIRST'
    elif competing_delay is not None and (fresh is None or competing_delay<fresh):
        competing='INVALIDATION_FIRST'
    elif fresh is None:
        competing='NEITHER'
    else:competing='SAME_CLOSE_AMBIGUOUS'
    # Price for hypothetical evaluation only: next 1m bar's OPEN. No fee/fill.
    next_open=future[0][1] if future else None
    direction=int(episode['first_direction_sign'])
    signed_closes=[v3.directed_pct(b[4],next_open,ev['side'],direction) for b in future] if next_open else []
    # Wick-based extremes are magnitude only; timing unknown within a bar.
    sign=v3.is_forward_side(ev['side'],direction)
    excursions=[v3.edge_prices(b,sign) for b in future]
    mfe=max([0]+[v3.directed_pct(g,next_open,ev['side'],direction) for g,_ in excursions]) if full else None
    mae=max([0]+[-v3.directed_pct(a,next_open,ev['side'],direction) for _,a in excursions]) if full else None
    race05,delay05,_=v3.race_on_closes(signed_closes,.5)
    race10,_,_=v3.race_on_closes(signed_closes,1.0)
    return dict(touch_key=ev['key'],symbol=ev['symbol'],date_utc=datetime.fromtimestamp(ev['ts']/1000,timezone.utc).date().isoformat(),
        cohort_type='FAILED' if ev['status']=='TOUCH_FAILED' else 'MATCHED_NONFAILED',
        observation_type=ev['observation'],original_side=ev['side'],impulse_direction=episode['impulse_direction'],
        pathway=state,early_state=obs['early_state'],era=cohort_era(ev,obs),
        second_min=int(episode['second_min']),known_min=s,threshold_pct=THRESHOLD,early_minute=LANDMARK,
        horizon=horizon,observed_future_bars=len(future),full_coverage=full,
        frozen_peak_price=episode['frozen_peak_price'],m5_baseline_price=episode['baseline_m5_price'],
        second_break_close=episode['second_close_price'],landmark_close=obs['landmark_close_price'],
        next_open_reference=next_open,progress_at_t1_pct=signed_progress(obs['landmark_close_price'],episode,ev['side']),
        frozen_peak_progress_pct=pk,known_extreme_progress_pct=observed_extreme,
        fresh_break_progress_threshold_pct=fresh_threshold,
        fresh_break_price_threshold=price_at_progress(episode,ev['side'],fresh_threshold),
        progress_to_fresh_break_pp=fresh_threshold-signed_progress(obs['landmark_close_price'],episode,ev['side']),
        progress_to_peak_pp=signed_progress(obs['landmark_close_price'],episode,ev['side'])-pk,
        first_fresh_break_delay_min=fresh if full else None,
        first_peak_recross_delay_min=invalid if full else None,
        first_baseline_cross_delay_min=baseline if full else None,
        baseline_already_at_t1=baseline_already_at_t1,
        competing_result=competing if full else 'CENSORED',
        first_0p5_from_next_open=race05 if full else 'CENSORED',
        first_0p5_delay=delay05 if full else None,
        first_1p0_from_next_open=race10 if full else 'CENSORED',
        next_open_mfe_wick_pct=mfe,next_open_mae_wick_pct=mae,
        # Only diagnostic: a 0.5% drop from t1 does not imply a fresh break.
        landmark_first_0p5=v5.forward_window(ev,episode,obs,horizon)['first_0p5'] if full else 'CENSORED')


def followthrough(ev,episode,obs,win,follow_horizon):
    """Evaluate AFTER a confirmed fresh break; not a time-t1 predictor."""
    hit=win.get('first_fresh_break_delay_min')
    if not win['full_coverage'] or win['competing_result']!='FRESH_BREAK_FIRST' or hit is None:return None
    hit_min=int(obs['known_min'])+hit
    b=ev['bars'];after=b[hit_min:hit_min+follow_horizon]
    full=len(after)==follow_horizon
    if not after:
        return dict(touch_key=ev['key'],pathway=win['pathway'],cohort_type=win['cohort_type'],
            horizon=win['horizon'],follow_horizon=follow_horizon,full_coverage=False,
            first_0p5='CENSORED',first_1p0='CENSORED',future_mfe_wick_pct=None,future_mae_wick_pct=None)
    opening=after[0][1]
    sign=int(episode['first_direction_sign']);price_sign=v3.is_forward_side(ev['side'],sign)
    values=[v3.directed_pct(bar[4],opening,ev['side'],sign) for bar in after]
    r05,delay05,_=v3.race_on_closes(values,.5)
    r10,_,_=v3.race_on_closes(values,1.0)
    extrema=[v3.edge_prices(bar,price_sign) for bar in after]
    mfe=max([0]+[v3.directed_pct(x,opening,ev['side'],sign) for x,_ in extrema]) if full else None
    mae=max([0]+[-v3.directed_pct(y,opening,ev['side'],sign) for _,y in extrema]) if full else None
    return dict(touch_key=ev['key'],symbol=ev['symbol'],cohort_type=win['cohort_type'],
        observation_type=ev['observation'],date_utc=win['date_utc'],era=win['era'],
        pathway=win['pathway'],horizon=win['horizon'],follow_horizon=follow_horizon,
        first_fresh_break_delay_min=hit,first_new_bar_open=opening,
        full_coverage=full,first_0p5=r05 if full else 'CENSORED',first_0p5_delay=delay05 if full else None,
        first_1p0=r10 if full else 'CENSORED',future_mfe_wick_pct=mfe,future_mae_wick_pct=mae)


def summarize(rows,fields):
    groups=defaultdict(list)
    for r in rows:groups[tuple(r[f] for f in fields)].append(r)
    out=[]
    for key,group in sorted(groups.items(),key=lambda q:str(q[0])):
        eligible=[r for r in group if r['full_coverage']]
        n=len(eligible)
        count=lambda k:sum(r['competing_result']==k for r in eligible)
        pct=lambda k:round(100*count(k)/n,3) if n else None
        med=lambda k:v3.median(r.get(k) for r in eligible)
        out.append(dict(zip(fields,key))|dict(N=len(group),N_full=n,N_censored=len(group)-n,
            n_fresh_first=count('FRESH_BREAK_FIRST'),n_invalidation_first=count('INVALIDATION_FIRST'),
            n_neither=count('NEITHER'),n_ambiguous=count('SAME_CLOSE_AMBIGUOUS'),
            n_baseline_already_t1=count('BASELINE_ALREADY_AT_T1'),
            pct_fresh_first=pct('FRESH_BREAK_FIRST'),pct_invalidation_first=pct('INVALIDATION_FIRST'),
            pct_continue_0p5_from_next_open=round(100*sum(r['first_0p5_from_next_open']=='CONTINUE' for r in eligible)/n,3) if n else None,
            n_0p5_from_landmark_without_fresh=sum(r['landmark_first_0p5']=='CONTINUE' and
                r['first_fresh_break_delay_min'] is None for r in eligible),
            med_delay_fresh=med('first_fresh_break_delay_min'),
            med_next_open_mfe=med('next_open_mfe_wick_pct'),med_next_open_mae=med('next_open_mae_wick_pct')))
    return out


def paired_audit(matches,records):
    idx={(r['touch_key'],r['horizon']):r for r in records}
    res=[]
    for m in matches:
        f=idx.get((m['failed_key'],15));c=idx.get((m['control_key'],15))
        if not f or not c:continue
        ok=f['full_coverage'] and c['full_coverage']
        same=ok and f['pathway']==c['pathway']
        res.append(dict(failed_key=f['touch_key'],control_key=c['touch_key'],
           failed_pathway=f['pathway'],control_pathway=c['pathway'],both_full=ok,
           both_same_pathway=same,failed_first=f['competing_result'],control_first=c['competing_result'],
           failed_era=f['era'],control_era=c['era']))
    return res


def run(project):
    project=Path(project)
    rd=project/'reports/reaction_lab'
    events=v2.read_all_first_touches(rd/'scanner_first_touches_v1.sqlite3',rd/'first_touch_paths_v2.sqlite3')
    # Controls matched using their FIRST FIVE minutes, before either second break.
    failed=[e for e in events if e['status']=='TOUCH_FAILED']
    other=[e for e in events if e['status'] in ('REACTION','TOUCH_INDECISIVE')]
    matches=v2.nearest_controls([q for x in failed if (q:=v2.observation(x,5))],
                                [q for x in other if (q:=v2.observation(x,5))])
    chosen={m['control_key'] for m in matches}
    selected=[*failed,*(e for e in other if e['key'] in chosen)]
    records=[];follow=[];episodes=[]
    for ev in selected:
        if ev['side']!='LONG':continue
        ep=v4.recognize(ev,THRESHOLD)
        if not ep or ep.get('impulse_direction')!='BREAKOUT':continue
        episodes.append(dict(touch_key=ev['key'],symbol=ev['symbol'],
                             cohort_type='FAILED' if ev['status']=='TOUCH_FAILED' else 'MATCHED_NONFAILED',
                             date_utc=datetime.fromtimestamp(ev['ts']/1000,timezone.utc).date().isoformat(),
                             second_status=ep['second_status'],observation_type=ev['observation']))
        if ep['second_status']!='SECOND_BREAKOUT':continue
        obs=v5.early_landmark(ev,ep,LANDMARK)
        if not obs:continue
        for horizon in HORIZONS:
            row=measure_window(ev,ep,obs,horizon)
            records.append(row)
            if horizon==15:
                for fwd in FOLLOW_HORIZONS:
                    hit=followthrough(ev,ep,obs,row,fwd)
                    if hit:follow.append(hit)
    output=project/OUT_REL
    sums=summarize(records,('cohort_type','era','pathway','horizon'))
    daily=summarize(records,('date_utc','cohort_type','pathway','horizon'))
    pairs=paired_audit(matches,records)
    v2.rows_to_csv(output/'v6_paths.csv',records)
    v2.rows_to_csv(output/'v6_summary.csv',sums)
    v2.rows_to_csv(output/'v6_daily.csv',daily)
    v2.rows_to_csv(output/'v6_fresh_break_followthrough.csv',follow,
        columns=list(follow[0]) if follow else ['touch_key','pathway','follow_horizon','full_coverage','first_0p5'])
    v2.rows_to_csv(output/'v6_pair_audit.csv',pairs,
        columns=list(pairs[0]) if pairs else ['failed_key','control_key','both_full','both_same_pathway'])
    v2.rows_to_csv(output/'v6_episodes.csv',episodes)
    focus=[x for x in sums if x['cohort_type']=='FAILED' and x['horizon']==15 and
           x['pathway'] in ('A_ACCELERATED','B_LOST_PEAK')]
    report=dict(generated_utc=datetime.now(timezone.utc).isoformat(),
      selection_frozen='LONG/BREAKOUT, second-break close, threshold 0.5%, landmark 1m; paths A=EXTENDED_0P25, B=LOST_PEAK',
      cutoff_utc=FREEZE_UTC,discovery_is_historical_not_oos=True,failed_total=len(failed),
      matched_controls=len(chosen),episode_count=len(episodes),n_audited_events=len({r['touch_key'] for r in records}),
      n_strict_paired_same_path=sum(x['both_same_pathway'] for x in pairs),
      focus_15m=focus,outputs=str(output))
    output.mkdir(parents=True,exist_ok=True)
    (output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    overview='\n'.join(f"- {r['era']} {r['pathway']}: {r['n_fresh_first']}/{r['N_full']} fresh first, "
                       f"{r['n_invalidation_first']}/{r['N_full']} invalidation first "
                       f"({r['N_censored']} censored)." for r in focus)
    (output/'ANALYSIS.md').write_text(
      '# FAILED Path Geometry Audit V6 — prueba de cierre, no optimización\n\n'
      '**Sin PnL, señales, operaciones ni modificación de LIVE.**\n\n'
      'Regla exploratoria congelada: LONG → BREAKOUT, segundo quiebre, t1. '
      'A=EXTENDED_0P25; B=LOST_PEAK. No crear filtros adicionales en la misma cohorte.\n\n'
      '## Primera carrera futura (por cierres 1m)\n'
      '- A: supera el extremo de cierre ya conocido en t1 + 0,05 pp, antes de cerrar otra vez del lado inválido del pico congelado.\n'
      '- B: supera el pico congelado + 0,05 pp, antes de regresar a la base m5.\n'
      '- Un +0,5% desde t1 en B NO equivale a recuperar el mínimo anterior.\n'
      '- Referencia adicional: OPEN de la primera vela FUTURA (no afirma fill).\n'
      '- Posterior al nuevo quiebre, las ventanas empiezan en la apertura de la siguiente vela.\n'
      '- Censura estricta para cada horizonte. Ambos lados de la carrera son por cierre 1m.\n'
      '- Conteos por día y símbolo, pares restringidos, y observaciones LIVE posteriores al corte separados.\n\n'
      '## Foco FAILED, 15 minutos\n'+overview+'\n\n'
      '## Criterio de cierre de investigación\n'
      '- Si la mayoría de aparentes continuaciones de B no recuperan el extremo, abandonar ese camino.\n'
      '- Si A no se repite en eventos FORWARD_LIVE de días/símbolos distintos, abandonar el filtro.\n'
      '- Recolección prospectiva necesaria antes de PnL o nuevas combinaciones.\n'
      '- Market Flow archivado no reconstruye eventos anteriores; no usar contexto actual.\n',
      encoding='utf-8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true');mode.add_argument('--run',action='store_true')
    a=p.parse_args();project=a.project.resolve()
    if a.check:
        es=v2.read_all_first_touches(project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3',
                                     project/'reports/reaction_lab/first_touch_paths_v2.sqlite3')
        print(json.dumps({'check':'OK_READ_ONLY','first_touches':len(es),
           'failed':sum(e['status']=='TOUCH_FAILED' for e in es),
           'freeze_utc':FREEZE_UTC,'selection':'LONG/BREAKOUT,0.5%,t1'},indent=2))
    else:
        print(json.dumps(run(project),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
