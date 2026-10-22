#!/usr/bin/env python3
"""Read-only examination of Atlas V2 origin and lifecycle before REACTION.

Does not touch TP/SL, produce files, or change the lifecycle classes.
"""
from pathlib import Path
from contextlib import closing
import argparse
import pandas as pd
from reaction_horizontal_lifecycle_meaning import meaning_label, origin_consistency


def utc(ts):
    try:
        return pd.to_datetime(int(float(ts)), unit='ms', utc=True).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, TypeError, OverflowError):
        return '—'


def _display_last_closed_htf(base, event, n=10):
    """Read-only native/assembled HTF as used by Atlas; no future candles."""
    import reaction_structural_level_atlas_v2 as atlas
    step={'15m':900000,'30m':1800000}.get(str(event.timeframe))
    if not step:
        return
    root = Path(base)
    ctxpath=root/atlas.v1.lineage.CTX_NAME
    cachepath=root/atlas.v1.lineage.HISTORY_NAME
    source_cache=atlas.v1.lineage.ro(cachepath) if cachepath.is_file() else None
    try:
        with closing(atlas.v1.lineage.ro(ctxpath)) as ctx:
            candles15,candles30,_=atlas.v1.v4._merge_tf(ctx,source_cache,str(event.symbol))
            candles = candles15 if str(event.timeframe)=='15m' else candles30
            candles=candles.reset_index().sort_values('timestamp')
            candles=candles.loc[(candles.timestamp+step)<=int(event.retest_ts)].tail(n)
            print(f'ÚLTIMAS {n} VELAS HTF CERRADAS PRE:')
            for b in candles.itertuples(index=False):
                c=float(b.close); low=float(event.zone_low); high=float(event.zone_high)
                side='ABOVE' if c>high else 'BELOW' if c<low else 'INSIDE'
                print(f' {utc(int(b.timestamp)+step)}  '
                      f'O={float(b.open):.9g} H={float(b.high):.9g} '
                      f'L={float(b.low):.9g} C={c:.9g} lado={side}')
    except Exception as exc:
        print('No se pudieron inspeccionar velas HTF (la etiqueta original permanece intacta):',exc)
    finally:
        if source_cache is not None:
            source_cache.close()


def inspect(root, symbols, show_bars=0):
    base = Path(root) / 'reports/reaction_lab'
    rows = pd.read_csv(base/'horizontal_level_lifecycle_pre_v1/event_lifecycle.csv',low_memory=False)
    rows = rows.loc[rows.tf_filter.eq('TODOS')].copy()
    zone = pd.read_csv(base/'reaction_structural_level_atlas_v2/candidate_levels.csv',low_memory=False)
    zone = zone[['event_key','zone_id','origin_kind','level_origin','formation_closed_ts']].rename(
        columns={'origin_kind':'pivot_origin','level_origin':'original_level_origin',
                 'formation_closed_ts':'origin_confirmed_ts'})
    joined = rows.merge(zone, on=['event_key','zone_id'],how='left',validate='one_to_one')
    interesting = joined.loc[joined.lifecycle_state.eq('NIVEL_RECUPERADO_PRE') |
                             joined.symbol.astype(str).str.upper().isin(symbols)]
    if interesting.empty:
        print('No hay eventos recuperados ni de los símbolos solicitados.')
    print('=== AUDITORÍA SEMÁNTICA PRE: TODOS, SIN USAR OUTCOMES ===')
    print(f'Eventos TOTAL={len(joined)}; RECUPERADOS={int(rows.lifecycle_state.eq("NIVEL_RECUPERADO_PRE").sum())}')
    for v in interesting.sort_values(['symbol','retest_ts']).itertuples(index=False):
        print('\n'+('='*64))
        print('EVENTO:',v.symbol,v.side,'|',v.event_key)
        print('REACTION open:', utc(v.retest_ts),'| nivel',v.timeframe,'|',v.zone_id)
        print('ORIGEN PIVOTE:',v.pivot_origin, '|',v.original_level_origin,
              '| formado:',utc(v.origin_confirmed_ts))
        print('BANDA:',v.zone_low,'-',v.zone_high, '| último cierre PRE:',v.last_pre_close,
              'en',utc(v.last_pre_close_ts))
        print('LADOS: inicial=',v.initial_side,'actual confirmado=',v.current_confirmed_side)
        print('FECHAS:', 'aceptación',utc(v.initial_acceptance_close_ts),
              '| pérdida',utc(v.last_break_close_ts),
              '| retest fallido',utc(v.last_failed_retest_close_ts),
              '| regreso',utc(v.last_reclaim_close_ts))
        print('ESTADO RAW:',v.lifecycle_state,'| DETALLE:',meaning_label(v.lifecycle_state,v.initial_side))
        print('ORIGEN VERSUS PRIMER LADO:',origin_consistency(v.initial_side,v.pivot_origin))
        print('COBERTURA:',v.history_quality,'| MOTIVO:',v.reason)
        if show_bars and str(v.symbol).upper() in symbols and str(v.lifecycle_state)!='SIN_NIVEL_IMPORTANTE_CERCANO_PRE':
            _display_last_closed_htf(base,v,n=show_bars)
    print('\nSin escrituras. Las etiquetas originales y First Touch no se modificaron.')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--project-root',default='.')
    p.add_argument('--symbols',default='XLMUSDT',help='Símbolos separados por coma; también lista todos los recuperados')
    p.add_argument('--show-bars',type=int,default=0,help='Mostrar hasta N velas HTF cerradas antes de los símbolos solicitados')
    args=p.parse_args()
    inspect(args.project_root,{s.strip().upper() for s in args.symbols.split(',') if s.strip()},show_bars=max(0,min(args.show_bars,30)))
