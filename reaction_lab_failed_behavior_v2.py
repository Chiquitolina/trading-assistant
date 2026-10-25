#!/usr/bin/env python3
"""FAILED Behavior Lab V2: sequences, matched first-touch controls, causal 4h context.

--run only reads source SQLites, writes isolated research reports.
--capture-flow-once reads the current CLOSED 4h Market Flow snapshot from Redis
and appends its FIRST observed version to a SEPARATE SQLite archive. It never
backdates historical availability. No PnL or order simulation.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import reaction_lab_failed_behavior_v1 as base

MINUTE = 60_000
LANDMARK = 5
HORIZONS = (30, 60)
THRESHOLDS = (.5, 1.0)
RETRACE_FRACTION = .5
BREAK_MARGIN = .05
MAX_CONTEXT_AGE_MS = 4 * 60 * MINUTE + 10 * MINUTE
ARCHIVE_REL = 'reports/reaction_lab/market_flow_history_4h_v1.sqlite3'
REPORT_REL = 'reports/reaction_lab/failed_behavior_v2'

FLOW_SQL = '''CREATE TABLE IF NOT EXISTS flow_snapshots (
  symbol TEXT NOT NULL, snapshot_close_ms INTEGER NOT NULL,
  observed_ms INTEGER NOT NULL, available_at_ms INTEGER NOT NULL,
  return_4h_pct REAL, return_rank_4h_pct REAL,
  relative_volume_4h REAL, volume_rank_4h_pct REAL,
  flow_group TEXT, primary_sector TEXT,
  sector_return_rank_pct_4h REAL, sector_strength_vs_btc_4h REAL,
  symbol_strength_vs_sector_4h REAL,
  PRIMARY KEY (symbol,snapshot_close_ms)
);
CREATE INDEX IF NOT EXISTS idx_flow_avail ON flow_snapshots(symbol,available_at_ms);'''


def num(x):
    return base.finite(x)


def rows_to_csv(path, records, columns=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    cols=list(columns or (records[0].keys() if records else ['no_rows']))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(records)


def capture_flow(project, *, now_ms=None):
    """Archive a snapshot NOW. Never assign earlier availability to past data."""
    from dashboard.services.market_flow_dashboard_service import MarketFlowDashboardService
    service=MarketFlowDashboardService(host=os.getenv('REDIS_HOST','127.0.0.1'),
        port=int(os.getenv('REDIS_PORT','6379')),db=int(os.getenv('REDIS_DB','0')))
    snapshot=service.get_snapshot('4h')
    if not snapshot:
        raise RuntimeError('No valid 4h Market Flow snapshot: '+str(getattr(service,'last_error','')))
    close=num(snapshot.get('market_flow_close_timestamp'))
    if close is None:raise ValueError('Market Flow snapshot without source-close timestamp; refusing archive')
    close=int(close)
    observed=int(now_ms) if now_ms is not None else int(time.time()*1000)
    if close>observed or observed-close>MAX_CONTEXT_AGE_MS:
        raise ValueError(f'Stale/future Market Flow: close={close} observed={observed}')
    frame=service.build_symbol_table(snapshot)
    if frame is None or len(frame)<20:
        raise ValueError('Insufficient cross-sectional coverage; refusing archive')
    records=[]
    for r in frame.to_dict('records'):
        sym=str(r.get('symbol') or '').strip().upper()
        if not sym:continue
        records.append((sym,close,observed,observed,
          num(r.get('return_pct_4h')),num(r.get('return_rank_pct_4h')),
          num(r.get('relative_volume_4h')),num(r.get('volume_rank_pct_4h')),
          str(r.get('flow_group') or ''),str(r.get('primary_sector') or ''),
          num(r.get('sector_return_rank_pct_4h')),
          num(r.get('sector_strength_vs_btc_4h')),
          num(r.get('symbol_strength_vs_sector_4h'))))
    if len(records)<20:raise ValueError('No valid symbol rows in Market Flow snapshot')
    out=Path(project)/ARCHIVE_REL;out.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(out,timeout=30)) as con:
        con.executescript(FLOW_SQL)
        before=con.total_changes
        con.executemany('''INSERT OR IGNORE INTO flow_snapshots VALUES
          (?,?,?,?,?,?,?,?,?,?,?,?,?)''',records)
        inserted=con.total_changes-before
        con.commit()
    return {'close_ms':close,'first_observed_ms':observed,'symbols_in_snapshot':len(records),
            'new_archived_symbols':inserted,'archive':str(out)}


def read_flow_asof(project, event_rows):
    """Only a snapshot *observed* before the event can be joined.

    Event timestamp = FAILED candle close, never first_seen_ms (recovered events
    were seen later). Snapshot archival arrival is conservative availability.
    """
    archive=Path(project)/ARCHIVE_REL
    if not archive.is_file():return {},'NO_ARCHIVE'
    with closing(base.ro_db(archive)) as con:
        flow=con.execute('SELECT * FROM flow_snapshots ORDER BY symbol,available_at_ms').fetchall()
        fields=[c[1] for c in con.execute('PRAGMA table_info(flow_snapshots)')]
    lookup=defaultdict(list)
    for row in flow:
        d=dict(zip(fields,row));lookup[d['symbol']].append(d)
    by_key={}
    for ev in event_rows:
        t=ev['ts']+MINUTE # fail candle known only after it closes
        # Conservative lag avoids using flow captured at exact bar-boundary.
        options=[r for r in lookup.get(ev['symbol'],[]) if r['available_at_ms']<=t
                 and r['snapshot_close_ms']<=t
                 and 0<=t-r['snapshot_close_ms']<=MAX_CONTEXT_AGE_MS]
        if options:by_key[ev['key']]=max(options,key=lambda r:(r['snapshot_close_ms'],r['available_at_ms']))
    return by_key,'OBSERVED_ARCHIVE'


def read_all_first_touches(journal,paths):
    """Read all categories for controls with the same strict path verification."""
    with closing(base.ro_db(journal)) as con:
        rows=con.execute('''SELECT touch_key,symbol,side,status,retest_ts,first_seen_ms,
              observation_type,causal_json FROM first_touches
              ORDER BY retest_ts,touch_key''').fetchall()
    with closing(base.ro_db(paths)) as con:
        path_rows={r[0]:r for r in con.execute('''SELECT touch_key,status,state,count,
           entry_ts,path_blob FROM paths''')}
    events=[]
    for key,symbol,side,status,ts,seen,obs,payload in rows:
        meta=json.loads(payload)
        if not isinstance(meta,dict):continue
        path=path_rows.get(key);bars=[];state='NOT_CAPTURED'
        if path:
            _,pst,state,n,entry,blob=path
            if pst!=status:raise ValueError('Status mismatch: '+key)
            bars=base.decode(blob)
            if n!=len(bars) or entry!=ts+MINUTE or any(b[0]!=entry+i*MINUTE for i,b in enumerate(bars)):
                raise ValueError('Noncontiguous path: '+key)
            for b in bars:
                if not all(math.isfinite(v) for v in b[1:]) or b[1]<=0 or b[3]<=0 or b[2]<max(b[1],b[4],b[3]) or b[3]>min(b[1],b[4],b[2]):
                    raise ValueError('Invalid OHLC: '+key)
        events.append(dict(key=key,symbol=symbol,side=side,status=status,ts=int(ts),
            seen_ms=int(seen),observation=obs,meta=meta,bars=bars,state=state))
    return events


def side_move(price,baseline,side):
    """Positive means direction of the FAILED breakout (inverse of original side)."""
    return (baseline-price)/baseline*100 if side=='LONG' else (price-baseline)/baseline*100


def observation(ev, landmark=LANDMARK):
    """Known-by-landmark predictors. No futures, no event outcomes."""
    if len(ev['bars'])<landmark or ev['side'] not in ('LONG','SHORT'):return None
    bars=ev['bars'];start=base.positive(ev['meta'].get('retest_close'))
    if not start:return None
    level=base.positive(ev['meta'].get('swing_price'))
    last=bars[landmark-1][4]
    returns=[side_move(b[4],start,ev['side']) for b in bars[:landmark]]
    first=[start]+[b[4] for b in bars[:landmark]]
    ranges=[(b[2]-b[3])/b[1]*100 for b in bars[:landmark]]
    # Fields designed to be equally available for non-FAILED first-touch controls.
    return dict(touch_key=ev['key'],symbol=ev['symbol'],status=ev['status'],
      original_side=ev['side'],observation_type=ev['observation'],retest_ts=ev['ts'],
      date_utc=datetime.fromtimestamp(ev['ts']/1000,timezone.utc).date().isoformat(),
      bars_saved=len(bars),landmark=landmark,
      baseline_price=last,first_touch_close=start,
      move_5m_break_pct=returns[-1],abs_move_5m_pct=abs(returns[-1]),
      range_5m_pct=sum(ranges),
      realized_5m_abs_change_pct=sum(abs((first[i+1]-first[i])/first[i]*100) for i in range(landmark)),
      # Level traits are meaningful for FAILED; not used in matching controls.
      penetration_at_fail_pct=base.break_distance(start,level,ev['side']) if level else None,
      dist_to_swing_m5_pct=base.break_distance(last,level,ev['side']) if level else None,
      all_first_5_closes_outside=bool(level and all(base.break_distance(b[4],level,ev['side'])>.1 for b in bars[:landmark])))


def pattern(ev, landmark=5, horizon=30, threshold=.5, retrace_frac=.5):
    """Close-confirmed impulse -> 50% pullback -> second leg or baseline cross.

    All timestamps are observation times. Peak freezes at pullback confirmation;
    later breach of frozen peak is second leg. No use of high/low intrabar order.
    If insufficient path to horizon, the unresolved states are CENSORED.
    """
    obs=observation(ev,landmark)
    if obs is None:return None
    b=ev['bars'];base_price=obs['baseline_price'];side=ev['side']
    n=min(horizon,max(0,len(b)-landmark))
    full=n>=horizon
    phase='WAIT_IMPULSE';first_dir=0;peak=0.0;trough=None
    impulse_t=None;peak_t=None;pullback_t=None;second_t=None
    price_at_pullback=None;impulse_threshold_time=None
    resolution='NONE'
    for i in range(landmark,landmark+n):
        minute=i+1
        v=side_move(b[i][4],base_price,side)
        if phase=='WAIT_IMPULSE':
            if abs(v)>=threshold:
                first_dir=1 if v>0 else -1
                peak=first_dir*v;impulse_t=minute;peak_t=minute
                phase='WAIT_PULLBACK'
        elif phase=='WAIT_PULLBACK':
            progress=first_dir*v
            if progress>peak:
                peak=progress;peak_t=minute
            elif peak-progress>=retrace_frac*peak:
                pullback_t=minute;trough=progress
                price_at_pullback=b[i][4]
                phase='WAIT_SECOND'
                # A strong reversal may cross the baseline in the SAME
                # candle that first confirms the pullback. This is already
                # observable at its close and must not be lost.
                if progress<=0:
                    second_t=minute;resolution='BASELINE_CROSSED';phase='RESOLVED'
        elif phase=='WAIT_SECOND':
            progress=first_dir*v
            trough=min(trough,progress)
            if progress>=peak+BREAK_MARGIN:
                second_t=minute;resolution='SECOND_BREAKOUT';phase='RESOLVED'
            elif progress<=0:
                second_t=minute;resolution='BASELINE_CROSSED';phase='RESOLVED'
        if phase=='RESOLVED':break
    if phase!='RESOLVED':
        state={'WAIT_IMPULSE':'NO_IMPULSE','WAIT_PULLBACK':'IMPULSE_NO_PULLBACK',
               'WAIT_SECOND':'PULLBACK_UNRESOLVED'}[phase]
        resolution=state if full else 'CENSORED_'+state
    return dict(touch_key=ev['key'],symbol=ev['symbol'],status=ev['status'],
      original_side=ev['side'],observation_type=ev['observation'],retest_ts=ev['ts'],
      landmark=landmark,horizon=horizon,threshold_pct=threshold,
      pullback_fraction=retrace_frac,available_post_landmark=n,full_coverage=full,
      impulse_direction=('BREAKOUT' if first_dir==1 else 'REVERSAL' if first_dir==-1 else 'NO_IMPULSE'),
      impulse_min=impulse_t,peak_min=peak_t,
      impulse_peak_pct=round(peak,6) if impulse_t is not None else None,
      pullback_min=pullback_t,pullback_fraction_realized_pct=(round(100*(peak-trough)/peak,3) if peak and trough is not None else None),
      pullback_price=price_at_pullback,second_resolution_min=second_t,
      resolution=resolution,
      minutes_from_impulse_to_pullback=(pullback_t-impulse_t if pullback_t and impulse_t else None),
      minutes_from_pullback_to_resolution=(second_t-pullback_t if second_t and pullback_t else None))


def nearest_controls(failed_obs, controls_obs, max_per_event=1, max_day_diff=0):
    """Matched NONFAILED first touches; NEVER claim generic/random-bar baseline.

    No matching on future outcome. Same original side and UTC date mandatory;
    prefer same symbol then nearest log distance in 5m movement/volatility.
    Controls cannot reuse another FAILED's path or overlap target by <=65m.
    Distances are non-optimized, transparent diagnostics.
    """
    controls_by_date=defaultdict(list)
    for c in controls_obs:
        controls_by_date[(c['date_utc'],c['original_side'])].append(c)
    matches=[]
    used=set()
    for f in sorted(failed_obs,key=lambda x:(x['retest_ts'],x['touch_key'])):
        candidates=[]
        for c in controls_by_date.get((f['date_utc'],f['original_side']),[]):
            if c['touch_key'] in used or abs(c['retest_ts']-f['retest_ts']) < 65*MINUTE:continue
            diff=(abs(c['move_5m_break_pct']-f['move_5m_break_pct'])/
                  max(.2,f['range_5m_pct']))
            vol=abs(math.log((c['range_5m_pct']+.05)/(f['range_5m_pct']+.05)))
            # Do not match unrelated first-touch shapes at any price.
            if diff>2 or vol>1.4:continue
            score=diff+vol+(0 if c['symbol']==f['symbol'] else .4)
            candidates.append((score,abs(c['retest_ts']-f['retest_ts']),c['touch_key'],c))
        if not candidates:continue
        candidates.sort(key=lambda x:x[:3])
        for score,delta,key,c in candidates[:max_per_event]:
            used.add(key)
            matches.append(dict(failed_key=f['touch_key'],control_key=key,
              symbol_failed=f['symbol'],symbol_control=c['symbol'],
              status_control=c['status'],date_utc=f['date_utc'],
              same_symbol=f['symbol']==c['symbol'],
              match_score=round(score,5),time_gap_min=delta/MINUTE,
              failed_move_5m=f['move_5m_break_pct'],control_move_5m=c['move_5m_break_pct'],
              failed_range_5m=f['range_5m_pct'],control_range_5m=c['range_5m_pct']))
    return matches


def summarize(records,groupby):
    out=[];parts=defaultdict(list)
    for r in records:
        key=tuple(r.get(x) for x in groupby);parts[key].append(r)
    for key,group in sorted(parts.items(),key=lambda z:str(z[0])):
        full=[r for r in group if r['full_coverage']]
        completed=[r for r in full if r['resolution'] in ('SECOND_BREAKOUT','BASELINE_CROSSED')]
        n=len(full)
        d=dict(zip(groupby,key));d.update(
            n_rows=len(group),n_complete=n,n_censored=len(group)-n,
            impulse_detected=sum(r['impulse_min'] is not None for r in full),
            pullback_detected=sum(r['pullback_min'] is not None for r in full),
            second_breakout=sum(r['resolution']=='SECOND_BREAKOUT' for r in full),
            baseline_cross=sum(r['resolution']=='BASELINE_CROSSED' for r in full),
            no_impulse=sum(r['resolution']=='NO_IMPULSE' for r in full),
            impulse_no_pullback=sum(r['resolution']=='IMPULSE_NO_PULLBACK' for r in full),
            pullback_unresolved=sum(r['resolution']=='PULLBACK_UNRESOLVED' for r in full),
            pct_impulse=round(100*sum(r['impulse_min'] is not None for r in full)/n,2) if n else None,
            pct_second_breakout=round(100*sum(r['resolution']=='SECOND_BREAKOUT' for r in full)/n,2) if n else None,
            pct_baseline_cross=round(100*sum(r['resolution']=='BASELINE_CROSSED' for r in full)/n,2) if n else None,
            median_time_to_impulse=base.pct(r['impulse_min']-r['landmark'] for r in full if r['impulse_min'] is not None)['median'],
            median_time_pullback=base.pct(r['minutes_from_impulse_to_pullback'] for r in full)['median'],
            median_peak_pct=base.pct(r['impulse_peak_pct'] for r in full)['median'])
        out.append(d)
    return out


def flow_bucket(v):
    if v is None:return 'UNAVAILABLE_ASOF'
    if v<20:return '0-20'
    if v<60:return '20-60'
    if v<80:return '60-80'
    return '80-100'


def run(project):
    project=Path(project)
    journal=project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
    paths=project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
    events=read_all_first_touches(journal,paths)
    failed=[e for e in events if e['status']=='TOUCH_FAILED']
    controls=[e for e in events if e['status'] in ('REACTION','TOUCH_INDECISIVE')]
    obs={e['key']:observation(e) for e in events}
    failed_obs=[obs[e['key']] for e in failed if obs[e['key']]]
    other_obs=[obs[e['key']] for e in controls if obs[e['key']]]
    matches=nearest_controls(failed_obs,other_obs)
    matched_keys={m['control_key'] for m in matches}
    match_failed_keys={m['failed_key'] for m in matches}
    by_key={e['key']:e for e in events}
    chosen_failed=failed
    chosen_controls=[by_key[k] for k in sorted(matched_keys)]
    asof,archive_status=read_flow_asof(project,events)
    sequences=[]
    for ev in chosen_failed+chosen_controls:
        o=obs[ev['key']]
        if not o:continue
        f=asof.get(ev['key'])
        for h in HORIZONS:
            for th in THRESHOLDS:
                s=pattern(ev,horizon=h,threshold=th)
                if s is None:continue
                s.update(cohort_type=('FAILED' if ev['status']=='TOUCH_FAILED' else 'MATCHED_NONFAILED'),
                  matched_failed=(ev['key'] in match_failed_keys),
                  all_first_5_closes_outside=o['all_first_5_closes_outside'],
                  early_move_pct=o['move_5m_break_pct'],
                  initial_penetration_pct=o['penetration_at_fail_pct'],
                  early_range_pct=o['range_5m_pct'],
                  flow_asof_available=f is not None,
                  flow_snapshot_close_ms=f['snapshot_close_ms'] if f else None,
                  flow_observed_ms=f['observed_ms'] if f else None,
                  flow_age_min=((ev['ts']+MINUTE-f['snapshot_close_ms'])/MINUTE if f else None),
                  return_4h_pct=f['return_4h_pct'] if f else None,
                  return_rank_4h_pct=f['return_rank_4h_pct'] if f else None,
                  volume_rank_4h_pct=f['volume_rank_4h_pct'] if f else None,
                  relative_volume_4h=f['relative_volume_4h'] if f else None,
                  flow_group=f['flow_group'] if f else 'UNAVAILABLE_ASOF',
                  primary_sector=f['primary_sector'] if f else 'UNAVAILABLE_ASOF',
                  rank_bucket=flow_bucket(f['return_rank_4h_pct'] if f else None),
                  volume_rank_bucket=flow_bucket(f['volume_rank_4h_pct'] if f else None))
                sequences.append(s)
    outfile=project/REPORT_REL
    rows_to_csv(outfile/'sequence_events.csv',sequences)
    rows_to_csv(outfile/'matched_controls.csv',matches)
    obs_rows=[]
    for o in failed_obs:
        f=asof.get(o['touch_key']);obs_rows.append(dict(o,flow_asof_available=f is not None,
          return_rank_4h_pct=f['return_rank_4h_pct'] if f else None,
          volume_rank_4h_pct=f['volume_rank_4h_pct'] if f else None,
          relative_volume_4h=f['relative_volume_4h'] if f else None,
          flow_group=f['flow_group'] if f else 'UNAVAILABLE_ASOF'))
    rows_to_csv(outfile/'failed_landmark_5m.csv',obs_rows)
    groups=[('cohort_type','original_side','horizon','threshold_pct'),
            ('cohort_type','all_first_5_closes_outside','horizon','threshold_pct'),
            ('cohort_type','observation_type','horizon','threshold_pct')]
    summary=[]
    for cols in groups:summary.extend(summarize(sequences,cols))
    rows_to_csv(outfile/'sequence_summary.csv',summary)
    flow_sub=[s for s in sequences if s['cohort_type']=='FAILED' and s['flow_asof_available']]
    flow_summary=summarize(flow_sub,('rank_bucket','flow_group','original_side','horizon','threshold_pct'))
    rows_to_csv(outfile/'flow_asof_summary.csv',flow_summary,
       columns=(list(flow_summary[0]) if flow_summary else ['rank_bucket','flow_group','original_side','horizon','threshold_pct','n_complete']))
    n_seq=sum(s['cohort_type']=='FAILED' and s['horizon']==30 and s['threshold_pct']==.5 and s['full_coverage'] for s in sequences)
    report=dict(generated_at_utc=datetime.now(timezone.utc).isoformat(),
        failed_in_journal=len(failed),failed_with_5m=len(failed_obs),
        failed_full_35m=n_seq,nonfailed_with_5m=len(other_obs),
        matched_pairs=len(matches),matched_failed=len(match_failed_keys),
        asof_archive_status=archive_status,
        failed_with_4h_context=sum(e['key'] in asof for e in failed),
        all_first_touches=len(events),outdir=str(outfile),
        warning='Matched controls are other first touches, NOT random market bars; sparse matches are not forced. Snapshot today without observed_at must NOT label historical failures.',
        definitions='t0 FAILED closes, t5 known close, future starts at t6; first abs signed threshold by CLOSE, track close peak, 50% peak pullback, second breakout > frozen peak+0.05pct or baseline cross; unresolved path = censored.')
    (outfile/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# FAILED Behavior Lab V2 — impulso, retroceso, segundo movimiento','',
      '**Investigación descriptiva. Sin PnL ni ejecución.**','',
      f"- FAILED en journal: **{len(failed)}**; con 5m: **{len(failed_obs)}**; 35m completos: **{n_seq}**.",
      f"- Comparadores REACTION/INDECISIVE con 5m: **{len(other_obs)}**; pares matched por misma fecha UTC y lado: **{len(matches)}**.",
      f"- FAILED con contexto 4h históricamente observable: **{report['failed_with_4h_context']}**. Snapshot actual sin hora NO se utiliza retrospectivamente.",
      '', '## Interpretación',
      '- La dirección del primer impulso se decide SOLO al primer cierre que cruza el umbral desde cierre minuto 5.',
      '- El primer pico es dinámico hasta la confirmación de retroceso de al menos 50% del impulso por cierre.',
      '- El segundo impulso requiere superar el pico congelado +0.05 puntos porcentuales antes de regresar a la base.',
      '- `NO_IMPULSE`, `IMPULSE_NO_PULLBACK`, `PULLBACK_UNRESOLVED` solo son definitivos con la ventana completa; de otro modo se censuran.',
      '- Los controles se seleccionan por mismo día UTC, lado, movimiento y rango conocidos hasta minuto 5; **no** representan todas las velas aleatorias del mercado.',
      '- Los contextos de Market Flow se unen solo si el snapshot 4h fue realmente archivado antes del evento y era reciente.',
      '- La dependencia por símbolo/fecha limita inferencias y toda selección de umbral es exploratoria.',
      '', '## Archivos',
      '- `sequence_events.csv`: diagnóstico individual por horizonte y umbral.',
      '- `sequence_summary.csv`: comparaciones por estado, dirección, persistencia y origen.',
      '- `matched_controls.csv`: pares y calidad del emparejamiento.',
      '- `failed_landmark_5m.csv`: atributos disponibles al minuto 5.',
      '- `flow_asof_summary.csv`: resultados por ranking y flow group **solo si hay snapshots causales**.',
      '- `report.json`: coberturas y advertencias.']
    (outfile/'ANALYSIS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path(__file__).resolve().parent)
    m=p.add_mutually_exclusive_group(required=True)
    m.add_argument('--check',action='store_true')
    m.add_argument('--run',action='store_true')
    m.add_argument('--capture-flow-once',action='store_true')
    m.add_argument('--capture-flow-loop',action='store_true')
    p.add_argument('--poll-seconds',type=int,default=120)
    args=p.parse_args()
    project=args.project.resolve()
    if args.check:
        j=project/'reports/reaction_lab/scanner_first_touches_v1.sqlite3'
        pa=project/'reports/reaction_lab/first_touch_paths_v2.sqlite3'
        ev=read_all_first_touches(j,pa)
        print(json.dumps({'check':'OK_NO_WRITE','touches':len(ev),'failed':sum(e['status']=='TOUCH_FAILED' for e in ev),
              'flows_archive_exists':(project/ARCHIVE_REL).is_file()},indent=2))
    elif args.run:print(json.dumps(run(project),indent=2,ensure_ascii=False))
    elif args.capture_flow_once:print(json.dumps(capture_flow(project),indent=2))
    else:
        while True:
            try:print('FLOW_CAPTURE',json.dumps(capture_flow(project),ensure_ascii=False),flush=True)
            except Exception as e:print('FLOW_CAPTURE_ERROR',repr(e),flush=True)
            time.sleep(max(60,args.poll_seconds))

if __name__=='__main__':main()
