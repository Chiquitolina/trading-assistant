#!/usr/bin/env python3
"""Render the two canonical V1.3 cases with CLOSED 15m/1h/4h bars.

Read-only input: the immutable V1.3 output, not the dashboard or LIVE.
Writes separate PRE and PRE+POST figures, in odd/even order, to a NEW folder.
Requires pandas, numpy and matplotlib. No image generation or price smoothing.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle, Patch
from matplotlib.lines import Line2D

BASE = Path('reports/reaction_lab/btc_multiswing_60d_v1')
DEFAULT_RUN = BASE / 'btc_sep09_nov08_structural_behavior_fixed_vs_asof_v13'
DEFAULT_OUTPUT = BASE / 'btc_sep09_nov08_structural_behavior_v13_case_figures'
M = 60_000
BG, PANEL, TEXT, GRID = '#0c1220', '#121d2e', '#e9eef8', '#2a384c'
UP, DOWN, BAND = '#48c9a4', '#f07476', '#dfb357'
SPECS = [
    dict(name='HIGH_20261022', title='HIGH 22/10 · cruce debajo y retest',
         region='msv2_f2cf7789ed5ecf4f4f81', cutoff=1792663380000,
         episode='msv2_f2cf7789ed5ecf4f4f81|EP|1792653540000', visit=19,
         markers={15:[('BREAK_BELOW_2C','2026-10-22 08:45','Cruce debajo · 2 cierres'),
                      ('RETEST_TOUCH','2026-10-22 10:15','Retest desde abajo'),
                      ('RETEST_HOLD_BELOW_2C','2026-10-22 10:30','Cierres debajo')],
                  60:[('BREAK_BELOW_2C','2026-10-22 10:00','Confirmación 1h · 75 min después')]}),
    dict(name='LOW_20261023', title='LOW 23/10 · intento de recuperación fallido',
         region='msv2_69f4a7f7cf54ab7a4214', cutoff=1792775940000,
         episode='msv2_69f4a7f7cf54ab7a4214|EP|1792722660000', visit=29,
         markers={15:[('BREAK_ABOVE_2C','2026-10-23 14:00','Cruce arriba · 2 cierres'),
                      ('FAILED_BREAK_RETURN_ORIGIN_2C','2026-10-23 14:30','Fallo: regreso debajo'),
                      ('DEFEND_ORIGIN_2C','2026-10-23 17:45','Defensa desde abajo')],
                  60:[('DEFEND_ORIGIN_2C','2026-10-23 19:00','Defensa 1h · 75 min después')]}),
]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def date_number(ms):
    return mdates.date2num(pd.to_datetime(ms,unit='ms',utc=True).to_pydatetime())


def band_steps(frame, region, start, end):
    g = frame.loc[frame.region_id.eq(region) & frame.known_from.le(end)].sort_values(['known_from','independent_extrema'])
    before = g.loc[g.known_from.le(start)].tail(1)
    after = g.loc[g.known_from.gt(start)]
    chosen = pd.concat([before,after])
    if chosen.empty:return [],[],[]
    t = [max(start,int(s)) for s in chosen.known_from]
    low, high = chosen.band_low.tolist(),chosen.band_high.tolist()
    t.append(end);low.append(low[-1]);high.append(high[-1])
    return date_number(np.array(t)),low,high


def render(spec, context, stages, events, sequences, output, post):
    cutoff = spec['cutoff']; end = cutoff + 12*60*M if post else cutoff
    fig, axes = plt.subplots(3,1,figsize=(15,12),facecolor=BG)
    fig.subplots_adjust(left=.072,right=.98,top=.88,bottom=.095,hspace=.36)
    state = 'PRE + POST de 12 h' if post else 'PRE · solo información cerrada al corte'
    fig.suptitle(spec['title'],x=.072,y=.972,ha='left',color=TEXT,fontsize=19,fontweight='bold')
    cuttext = pd.to_datetime(cutoff,unit='ms',utc=True).strftime('%d-%m-%Y %H:%M UTC')
    fig.text(.072,.935,f"{state}   |   Corte: {cuttext}   |   Visita canónica {spec['visit']}",color='#b8c7df',fontsize=11)
    fig.text(.072,.91,'Una misma región: interacción 15m, eventos 1h y contexto 4h. Velas ubicadas en su hora de cierre.',color='#a3b4ce',fontsize=10)
    ep = sequences.loc[sequences.episode_id.eq(spec['episode'])].iloc[0]
    selected_events = events.loc[events.episode_id.eq(spec['episode'])]
    marker_rows = []
    for ax, tf, days in zip(axes,(15,60,240),(1,7,14)):
        start = cutoff-days*1440*M
        ax.set_facecolor(PANEL)
        bars = context.loc[context.case_name.eq(spec['name']) & context.tf_minutes.eq(tf) &
                           context.closed_ts.ge(start) & context.closed_ts.le(end)].sort_values('closed_ts')
        x = date_number(bars.closed_ts.to_numpy())
        width = tf/1440*.72
        for xp,row in zip(x,bars.itertuples(index=False)):
            color = UP if row.close >= row.open else DOWN
            ax.vlines(xp,row.low,row.high,color=color,linewidth=.75,alpha=.95)
            height=abs(row.close-row.open)
            ax.add_patch(Rectangle((xp-width/2,min(row.open,row.close)),width,max(height,.4),
                                   facecolor=color,edgecolor=color,linewidth=.45))
        bx,lo,hi=band_steps(stages,spec['region'],start,end)
        if len(bx):
            ax.fill_between(bx,lo,hi,step='post',color=BAND,alpha=.20)
            ax.step(bx,lo,where='post',color=BAND,linewidth=.9)
            ax.step(bx,hi,where='post',color=BAND,linewidth=.9)
        # The fixed episode reference starts at the episode, never projected
        # onto older history before that reference was available.
        reference_start=max(start,int(ep.pre_cutoff_ts))
        ax.plot(date_number(np.array([reference_start,end])),[ep.stage_low,ep.stage_low],color='#a6b1c4',linewidth=.65,linestyle=':',alpha=.7)
        ax.plot(date_number(np.array([reference_start,end])),[ep.stage_high,ep.stage_high],color='#a6b1c4',linewidth=.65,linestyle=':',alpha=.7)
        cutx=date_number(cutoff)
        if post:ax.axvspan(cutx,date_number(end),color='#758fbf',alpha=.10)
        ax.axvline(cutx,color='#ffd36a',linestyle=(0,(4,4)),linewidth=1.1)
        ax.text(cutx,.99,'Corte PRE ',transform=ax.get_xaxis_transform(),color='#ffd36a',ha='right',va='top',fontsize=9)
        targets=spec['markers'].get(tf,[])
        for i,(kind,time,label) in enumerate(targets):
            ts=int(pd.Timestamp(time,tz='UTC').timestamp()*1000)
            if ts > end or ts < start:continue
            rows=selected_events.loc[selected_events.source_tf.eq('M15_CLOSED' if tf==15 else 'H1_CLOSED') &
                                     selected_events.event_type.eq(kind) & selected_events.confirmation_ts.eq(ts)]
            if rows.empty:raise ValueError('CASE_EVENT_NOT_REPRODUCED: '+str((kind,time)))
            row=rows.iloc[0];xp=date_number(ts);yp=float(row.confirmation_close)
            ax.scatter([xp],[yp],s=36,color='#a7d7ff',edgecolors=BG,linewidth=.6,zorder=5)
            # Offset close-together local events to keep labels readable.
            offset=25 if i==0 else -44 if i==1 else 45
            horizontal=-320 if tf==60 else -110 if i==0 else -145 if i==1 else 30
            ax.annotate(label,xy=(xp,yp),xytext=(horizontal,offset),textcoords='offset points',
                        color=TEXT,fontsize=8.7,ha='left',
                        bbox=dict(boxstyle='round,pad=.3',fc=BG,ec='#36516d',alpha=.96),
                        arrowprops=dict(arrowstyle='-',color='#83a6cc',linewidth=.8),zorder=6)
            marker_rows.append({'case_name':spec['name'],'figure_role':'PRE_POST' if post else 'PRE',
                                'source_tf':row.source_tf,'event_type':kind,'confirmation_ts':ts,
                                'confirmation_close':yp,'asof_transition_id':row.asof_transition_id})
        name = '15m · interacción local · últimas 24 h' if tf==15 else '1h · estructura e historia · últimos 7 días' if tf==60 else '4h · contexto cerrado · últimos 14 días'
        ax.set_title(name,loc='left',color=TEXT,fontsize=11,fontweight='bold',pad=10)
        ax.set_xlim(date_number(start),date_number(end)+tf/1440*.5)
        prices=np.r_[bars.low.to_numpy(),bars.high.to_numpy()]
        if len(prices):
            span=max(float(prices.max()-prices.min()),50)
            ax.set_ylim(float(prices.min())-.09*span,float(prices.max())+.16*span)
        ax.grid(True,color=GRID,linewidth=.45,alpha=.6)
        for spine in ax.spines.values():spine.set_color(GRID)
        ax.tick_params(colors='#b9c8df',labelsize=9)
        ax.set_ylabel('USD',color='#aebfd9',fontsize=9)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4,maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d-%b\n%H:%M',tz='UTC'))
        if tf==240:
            last_pre=bars.loc[bars.closed_ts.le(cutoff)].tail(1)
            if len(last_pre):
                text=pd.to_datetime(int(last_pre.closed_ts.iloc[0]),unit='ms',utc=True).strftime('%d-%b %H:%M UTC')
                ax.text(.012,.035,'Última 4h disponible en PRE: '+text,transform=ax.transAxes,color='#d3def0',fontsize=8.8)
    handles=[Patch(facecolor=BAND,alpha=.35,label='Límites confirmados as-of'),
             Line2D([],[],color='#a6b1c4',linestyle=':',label='Referencia fija desde inicio del episodio'),
             Line2D([],[],color='#a7d7ff',marker='o',linestyle='None',label='Evento V1.3 confirmado')]
    fig.legend(handles=handles,loc='lower left',bbox_to_anchor=(.067,.035),ncol=3,
               facecolor=BG,edgecolor=BG,labelcolor=TEXT,fontsize=9)
    fig.text(.072,.015,'POST separado por el corte. La banda describe geometría conocida; no certifica estado ACTIVE ni una operación.',color='#9bacc7',fontsize=9)
    fig.savefig(output,dpi=145,facecolor=BG)
    plt.close(fig)
    return marker_rows


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',type=Path,default=Path('.'))
    p.add_argument('--run-dir',type=Path,default=DEFAULT_RUN)
    p.add_argument('--output-dir',type=Path,default=DEFAULT_OUTPUT)
    args=p.parse_args(argv);root=args.project_root.resolve()
    run=(args.run_dir if args.run_dir.is_absolute() else root/args.run_dir).resolve()
    out=(args.output_dir if args.output_dir.is_absolute() else root/args.output_dir).resolve()
    if out==run or out.is_relative_to(run):raise ValueError('FIGURES_MUST_NOT_MODIFY_IMMUTABLE_RUN')
    if out.exists():raise FileExistsError('IMMUTABLE_FIGURE_FOLDER_ALREADY_EXISTS: '+str(out))
    mf=json.loads((run/'manifest.json').read_text())
    names=['case_context_closed_bars_v13.csv','case_geometric_stages_v13.csv',
           'case_chronologies_v13.csv','episode_sequences_v13.csv']
    for name in names:
        if digest(run/name)!=mf['files_sha256'][name]:raise ValueError('FIGURE_INPUT_HASH_MISMATCH: '+name)
    context,stages,events,sequences=[pd.read_csv(run/n,low_memory=False) for n in names]
    out.mkdir(parents=True)
    markers=[]
    for n,spec in enumerate(SPECS):
        for offset,post in enumerate((False,True),start=1):
            filename=f"{2*n+offset:02d}_{spec['name']}_{'PRE_POST' if post else 'PRE'}.png"
            markers.extend(render(spec,context,stages,events,sequences,out/filename,post))
    pd.DataFrame(markers).to_csv(out/'plotted_causal_events_v13.csv',index=False)
    files=sorted(out.iterdir())
    manifest={'input_v13_manifest_sha256':digest(run/'manifest.json'),
              'odd_figures':'PRE_ONLY','even_figures':'PRE_AND_POST_12H',
              'files_sha256':{f.name:digest(f) for f in files}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    print('MS_BEHAVIOR_V13_CASE_FIGURES_FINISHED',out)
    return 0


if __name__=='__main__':raise SystemExit(main())
