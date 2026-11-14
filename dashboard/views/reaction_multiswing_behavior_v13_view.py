"""Read-only interactive V1.3 episode view inside the existing BTC 60D lab.

No replay, detector, network, trading or dataset writes. PRE is filtered at
the payload level: candles, labels, links, comparison rows and downloads share
the same closed-data boundary. All Streamlit imports are render-time only.
"""
from __future__ import annotations

import hashlib
import json
import time
import textwrap
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

M = 60_000
DAY = 1440*M
BASE = Path('reports/reaction_lab/btc_multiswing_60d_v1')
RUN = BASE/'btc_sep09_nov08_structural_behavior_fixed_vs_asof_v13'
P12 = BASE/'btc_sep09_nov08_structural_behavior_15m_1h_v12'
P121 = BASE/'btc_sep09_nov08_structural_behavior_pre_safety_v121'
FREEZE = BASE/'btc_sep09_nov08_60d_v1'
CATALOG = BASE/'btc_sep09_nov08_swing_catalog_6tf_v1'
VERSION = 'btc-multiswing-structural-behavior-fixed-vs-causal-stage-v1.3'
PREFIX = 'ms60_v13_'
POST_KEY = PREFIX+'reveal_post'
PRESETS = ('LOW_23_OCT', 'HIGH_22_OCT', 'HIGH_18_SEP', 'FLIPS_PRE', 'CATALOG')
BOOKMARKS = {
    'LOW_23_OCT': ('msv2_69f4a7f7cf54ab7a4214', 'msv2_69f4a7f7cf54ab7a4214|EP|1792722660000',
                   'msv2_69f4a7f7cf54ab7a4214|V29|1792775940000'),
    'HIGH_22_OCT': ('msv2_f2cf7789ed5ecf4f4f81', 'msv2_f2cf7789ed5ecf4f4f81|EP|1792653540000',
                    'msv2_f2cf7789ed5ecf4f4f81|V19|1792663380000'),
    'HIGH_18_SEP': ('msv2_a90a10e0f9eff24e9799', 'msv2_a90a10e0f9eff24e9799|EP|1789709580000',
                    'msv2_a90a10e0f9eff24e9799|V1|1789709580000'),
}
INDEX_FILES = ((RUN,'episode_sequences_v13.csv'), (RUN,'asof_transition_observations_v13.csv'),
               (RUN,'h1_m15_links_asof_v13.csv'), (P12,'visit_pre_v12.csv'),
               (P121,'geometric_stage_history_v121.csv'))
STATUS_LABELS = {
    'SAME_LABEL_AND_LINEAGE': 'Misma etiqueta y ruptura asociada',
    'SAME_LABEL_DIFFERENT_LINEAGE': 'Misma etiqueta, otra ruptura asociada',
    'CHANGED_LABEL': 'Etiqueta diferente', 'REMOVED_UNDER_ASOF': 'No se emite en ese cierre',
    'INDETERMINATE': 'Datos insuficientes o ambiguos',
}


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def utc(timestamp):
    return pd.to_datetime(int(timestamp),unit='ms',utc=True).strftime('%Y-%m-%d %H:%M UTC')


def iso_values(values):
    return pd.to_datetime(np.asarray(values,dtype='int64'),unit='ms',utc=True).strftime('%Y-%m-%dT%H:%M:%SZ').tolist()


def signature(root, paths):
    return tuple((str(p), (root/p).stat().st_size, (root/p).stat().st_mtime_ns) for p in paths)


def manifest(folder, version=None):
    m=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if not m.get('immutable_run') or (version and m.get('version')!=version):
        raise ValueError('Manifiesto de estudio incorrecto: '+str(folder.name))
    return m


def verified_csv(folder, name, m):
    expected=m.get('files_sha256',{}).get(name)
    if not expected or sha256(folder/name)!=expected:
        raise ValueError('Archivo incompleto o modificado: '+name)
    return pd.read_csv(folder/name,low_memory=False)


@lru_cache(maxsize=2)
def _load_index(root_string, token):
    root=Path(root_string)
    run=manifest(root/RUN,VERSION)
    p12=manifest(root/P12)
    p121=manifest(root/P121)
    for label,path in [('v12',P12),('v121',P121)]:
        if run.get('parent_manifest_sha256',{}).get(label)!=sha256(root/path/'manifest.json'):
            raise ValueError('El run V1.3 no corresponde a los padres disponibles: '+label)
    frames=[verified_csv(root/path,name,run if path==RUN else p12 if path==P12 else p121)
            for path,name in INDEX_FILES]
    episodes,events,links,visits,stages=frames
    if (episodes.episode_id.duplicated().any() or events.asof_transition_id.duplicated().any() or
            visits.event_id.duplicated().any() or not set(events.episode_id)<=set(episodes.episode_id)):
        raise ValueError('Índice de episodios o transiciones inconsistente')
    return {'root':root,'manifest':run,'episodes':episodes,'events':events,
            'links':links,'visits':visits,'stages':stages}


def load_index(project):
    root=Path(project).resolve()
    paths=[RUN/'manifest.json',P12/'manifest.json',P121/'manifest.json']+[p/n for p,n in INDEX_FILES]
    return _load_index(str(root),signature(root,paths))


@lru_cache(maxsize=2)
def _load_prices(root_string, token):
    from btc_multiswing_60d_explorer_v1 import load_snapshot
    full,_,source=load_snapshot(Path(root_string))
    start,end=int(full.timestamp.iloc[0]),int(source['end'])
    bars={}
    for tf,name in [(15,'15m'),(60,'1h'),(240,'4h')]:
        bars[tf]=closed_price_bars(full,tf,start,end)
    return bars,source


def closed_price_bars(full, width_minutes, start, end):
    """UTC buckets in integer milliseconds, independent of datetime units.

    The verified minute source is contiguous. Count only complete buckets;
    retain the legacy epoch-aligned OHLC and close-time visibility contract.
    """
    width=int(width_minutes);step=width*M
    frame=full[['timestamp','open','high','low','close']]
    key=(frame.timestamp.astype('int64')//step)*step
    result=frame.groupby(key,sort=True).agg(open=('open','first'),high=('high','max'),
        low=('low','min'),close=('close','last'),count=('timestamp','size'))
    result=result.loc[result['count'].eq(width)].drop(columns='count')
    result.index.name='timestamp';result=result.reset_index()
    result['closed_ts']=result.timestamp+step
    return result.loc[result.closed_ts.gt(int(start))&result.closed_ts.le(int(end))].reset_index(drop=True)


def load_prices(project, study):
    root=Path(project).resolve()
    paths=[FREEZE/'manifest.json',FREEZE/'BTCUSDT_1m.csv',FREEZE/'BTCUSDT_1m_warmup.csv',
           CATALOG/'manifest.json',CATALOG/'confirmed_swings.csv']
    bars,source=_load_prices(str(root),signature(root,paths))
    for key in ('start','end','source_sha256','catalog_sha256'):
        if source[key]!=study['manifest']['source_provenance'].get(key):
            raise ValueError('La fuente congelada no corresponde al run V1.3: '+key)
    return bars,source


@lru_cache(maxsize=2)
def _load_comparison(root_string, token):
    root=Path(root_string)
    return verified_csv(root/RUN,'observation_comparison_v13.csv',manifest(root/RUN,VERSION))


def load_comparison(project):
    root=Path(project).resolve()
    return _load_comparison(str(root),signature(root,[RUN/'manifest.json',RUN/'observation_comparison_v13.csv']))


def clear_caches():
    _load_index.cache_clear();_load_prices.cache_clear();_load_comparison.cache_clear()
    _load_flip_catalog.cache_clear()


FLIP_COLUMNS = ['event_id','episode_id','region_id','pre_cutoff_ts','flip_direction','reaction_pre',
                'band_age_hours','first_known_ts','break_ts','break_event_id','prior_h1_defenses',
                'prior_h1_holds','max_separation_pct','max_separation_widths','m15_opposite_reactions',
                'h1_opposite_reactions','geometry_changed_since_break','pre_position','pre_h1_position',
                'pre_h4_position','pre_h4_return_12h_pct']


def build_flip_catalog(study, bars):
    """Descriptive PRE cohort from existing H1 evidence, never POST outcomes.

    For each historical visit: prior role defenses -> latest H1 BREAK in the
    opposite direction -> return from that side. Reject failed/recovered
    breaks. Measure separation using only closed H1 prices and their own
    contemporaneous stages. Missing evidence is not a confirmed flip.
    """
    events=study['events']
    required=['data_complete','parent_asof_transition_id','asof_physical_transition_id']
    if not all(c in events for c in required):raise ValueError('Faltan campos de evidencia para el filtro PRE')
    output=[]
    for region,visits in study['visits'].groupby('region_id',sort=False):
        stages=study['stages'].loc[study['stages'].region_id.eq(region)].sort_values(['known_from','independent_extrema'])
        stages=stages.drop_duplicates('known_from',keep='last')
        if stages.empty:continue
        stage_ts=stages.known_from.to_numpy(dtype='int64')
        low=stages.band_low.to_numpy(dtype=float);high=stages.band_high.to_numpy(dtype=float)
        all_events=events.loc[events.region_id.eq(region)].sort_values('confirmation_ts')
        known=all_events.data_complete.fillna(False).eq(True)&all_events.stage_known_from.le(all_events.confirmation_ts)
        known &= np.isfinite(all_events.band_low_asof)&np.isfinite(all_events.band_high_asof)
        good=all_events.loc[known].copy()
        h1=good.loc[good.source_tf.eq('H1_CLOSED')].copy()
        m15=good.loc[good.source_tf.eq('M15_CLOSED')].copy()
        hour_clocks=bars[60].closed_ts.to_numpy(dtype='int64')
        hour_prices=bars[60].close.to_numpy(dtype=float)
        hour_stage=np.searchsorted(stage_ts,hour_clocks,side='right')-1
        positions=np.full(len(hour_clocks),-9,dtype='int8')
        available=np.flatnonzero(hour_stage>=0)
        si=hour_stage[available]
        finite=np.isfinite(hour_prices[available])&np.isfinite(low[si])&np.isfinite(high[si])&(low[si]<high[si])
        available=available[finite];si=hour_stage[available]
        positions[available]=np.where(hour_prices[available]<low[si],-1,np.where(hour_prices[available]>high[si],1,0))
        previous_outside=np.full(len(hour_clocks),-9,dtype='int8');last_side=-9
        for n,p in enumerate(positions):
            if p in (-9,-1,1):last_side=int(p)
            previous_outside[n]=last_side
        def physical_hourly_cross(row):
            k=int(np.searchsorted(hour_clocks,int(row.confirmation_ts)))
            far=1 if row.direction=='ABOVE' else -1
            # Episode seeding can re-emit a break in an already long outside
            # streak. This cohort requires the real second consecutive H1
            # close, preceded by the opposite outside side (inside allowed).
            return bool(k>=2 and k<len(hour_clocks) and hour_clocks[k]==int(row.confirmation_ts)
                        and hour_clocks[k]-hour_clocks[k-1]==60*M
                        and positions[k]==far and positions[k-1]==far and positions[k-2]!=far
                        and previous_outside[k-2]==-far)
        break_rows=h1.loc[h1.event_type.isin(['BREAK_ABOVE_2C','BREAK_BELOW_2C'])]
        valid_cross_ids={row.asof_transition_id for row in break_rows.itertuples(index=False) if physical_hourly_cross(row)}
        # Alias map is used only after slicing to a visit's available prefix.
        for visit in visits.itertuples(index=False):
            cutoff=int(visit.pre_cutoff_ts)
            stage_index=int(np.searchsorted(stage_ts,cutoff,side='right'))-1
            if stage_index<0 or not np.isfinite([low[stage_index],high[stage_index]]).all() or low[stage_index]>=high[stage_index]:continue
            if int(visit.pre_last_1h_close_ts)>cutoff or int(visit.pre_last_1m_close_ts)>cutoff or int(visit.pre_last_4h_close_ts)>cutoff:continue
            history=h1.loc[h1.confirmation_ts.le(cutoff)]
            breaks=history.loc[history.asof_transition_id.isin(valid_cross_ids)]
            if breaks.empty:continue
            cross=breaks.iloc[-1]
            ts=int(cross.confirmation_ts);far=str(cross.direction)
            origin='BELOW' if far=='ABOVE' else 'ABOVE'
            if cross.origin_position!=origin:continue
            concurrent=breaks.loc[breaks.confirmation_ts.eq(ts)]
            if concurrent.direction.nunique()!=1:continue
            aliases=set(history.loc[history.asof_physical_transition_id.eq(cross.asof_physical_transition_id)].asof_transition_id)
            after=history.loc[history.confirmation_ts.gt(ts)]
            failed=after.event_type.str.startswith('FAILED_BREAK_')&after.parent_asof_transition_id.isin(aliases)
            recovered=after.event_type.isin(['BREAK_'+origin+'_2C','EXIT_'+origin+'_2C'])
            if failed.any() or recovered.any():continue
            # A later incomplete structural observation prevents a clean PRE
            # classification, rather than silently reviving an older break.
            incomplete=all_events.loc[~known & all_events.source_tf.eq('H1_CLOSED')
                                      & all_events.confirmation_ts.between(ts,cutoff)]
            if len(incomplete):continue
            def position(price):
                if not np.isfinite(price):return 'UNKNOWN'
                return 'BELOW' if price<low[stage_index] else 'ABOVE' if price>high[stage_index] else 'INSIDE'
            current=position(float(visit.pre_1m_close));hour_position=position(float(visit.pre_1h_close))
            if current not in (far,'INSIDE') or hour_position not in (far,'INSIDE'):continue
            before=history.loc[history.confirmation_ts.lt(ts)]
            defenses=before.loc[before.event_type.eq('DEFEND_ORIGIN_2C')&before.direction.eq(origin)]
            count=int(defenses.confirmation_ts.nunique())
            if not count:continue
            holds=before.loc[before.event_type.eq('RETEST_HOLD_'+origin+'_2C')]
            # All prices are closed by cutoff. No wick of the arrival hour is
            # included before that hour closes; no geometry from PRE is
            # projected backward to manufacture separation.
            q=bars[60].loc[bars[60].closed_ts.between(ts,cutoff)]
            indices=np.searchsorted(stage_ts,q.closed_ts.to_numpy(dtype='int64'),side='right')-1
            valid=indices>=0
            indices=indices[valid];closes=q.close.to_numpy(dtype=float)[valid]
            if not len(indices):continue
            edges=high[indices] if far=='ABOVE' else low[indices]
            widths=high[indices]-low[indices]
            deltas=closes-edges if far=='ABOVE' else edges-closes
            finite=np.isfinite(edges)&np.isfinite(widths)&np.isfinite(deltas)&(edges>0)&(widths>0)
            if not finite.all():continue
            separation=np.maximum(deltas,0)
            def reactions(frame):
                f=frame.loc[frame.confirmation_ts.gt(ts)&frame.confirmation_ts.le(cutoff)&frame.direction.eq(far)]
                return int(f.loc[f.event_type.isin(['DEFEND_ORIGIN_2C','RETEST_HOLD_'+far+'_2C'])].confirmation_ts.nunique())
            hr=reactions(h1);local=reactions(m15)
            output.append(dict(event_id=visit.event_id,episode_id=visit.episode_id,region_id=region,
                pre_cutoff_ts=cutoff,flip_direction='RESISTANCE_TO_SUPPORT' if far=='ABOVE' else 'SUPPORT_TO_RESISTANCE',
                reaction_pre='H1' if hr else 'M15' if local else 'PENDING',
                band_age_hours=(cutoff-int(stage_ts[0]))/(60*M),first_known_ts=int(stage_ts[0]),
                break_ts=ts,break_event_id=cross.asof_transition_id,prior_h1_defenses=count,
                prior_h1_holds=int(holds.confirmation_ts.nunique()),
                max_separation_pct=float(np.max(separation/edges*100)),
                max_separation_widths=float(np.max(separation/widths)),
                m15_opposite_reactions=local,h1_opposite_reactions=hr,
                geometry_changed_since_break=bool(low[stage_index]!=cross.band_low_asof or high[stage_index]!=cross.band_high_asof),
                pre_position=current,pre_h1_position=hour_position,pre_h4_position=position(float(visit.pre_4h_close)),
                pre_h4_return_12h_pct=float(visit.pre_4h_ret_12h_pct)))
    return pd.DataFrame(output,columns=FLIP_COLUMNS)


def filter_flip_catalog(frame, *, min_age_hours=72, min_defenses=2, min_separation_pct=.25,
                        direction='BOTH', reaction='M15_OR_H1', unique_bands=True):
    if frame.empty:return frame.copy()
    keep=frame.band_age_hours.ge(min_age_hours)&frame.prior_h1_defenses.ge(min_defenses)
    keep &= frame.max_separation_pct.ge(min_separation_pct)
    if direction!='BOTH':keep &= frame.flip_direction.eq(direction)
    if reaction=='H1':keep &= frame.reaction_pre.eq('H1')
    elif reaction=='M15_OR_H1':keep &= frame.reaction_pre.isin(['M15','H1'])
    result=frame.loc[keep].sort_values(['pre_cutoff_ts','event_id']).copy()
    if unique_bands:result=result.drop_duplicates('region_id',keep='first')
    return result.sort_values(['pre_cutoff_ts','event_id'],ascending=[False,True]).copy()


@lru_cache(maxsize=2)
def _load_flip_catalog(root_string, token):
    study=load_index(root_string);bars,_=load_prices(root_string,study)
    return build_flip_catalog(study,bars)


def load_flip_catalog(project):
    root=Path(project).resolve()
    paths=[RUN/'manifest.json',P12/'manifest.json',P121/'manifest.json']+[p/n for p,n in INDEX_FILES]
    paths += [FREEZE/'manifest.json',FREEZE/'BTCUSDT_1m.csv',FREEZE/'BTCUSDT_1m_warmup.csv',
              CATALOG/'manifest.json',CATALOG/'confirmed_swings.csv']
    return _load_flip_catalog(str(root),signature(root,paths))


FLIP_LABELS={'SUPPORT_TO_RESISTANCE':'Soporte → resistencia','RESISTANCE_TO_SUPPORT':'Resistencia → soporte'}
REACTION_LABELS={'H1':'Reacción opuesta registrada en 1h','M15':'Reacción opuesta 15m · 1h pendiente',
                 'PENDING':'Candidato · reacción opuesta pendiente'}


def render_flip_selector(st, project, width):
    catalog=load_flip_catalog(project)
    st.markdown('##### Casos con historia de cambio de función · filtro PRE')
    with st.expander('Filtros estructurales PRE'):
        a,b,c=st.columns(3)
        with a:age=_safe_select(st,'Antigüedad mínima de banda · horas',(0,24,48,72,168,336),PREFIX+'flip_age',72)
        with b:defenses=_safe_select(st,'Defensas registradas 1h previas · mínimo',(1,2,3,4),PREFIX+'flip_defenses',2)
        with c:distance=_safe_select(st,'Alejamiento cerrado 1h previo · mínimo %',(0.0,.1,.25,.5,1.0),PREFIX+'flip_distance',.25)
        a,b=st.columns(2)
        with a:direction=_safe_select(st,'Cambio de función',('BOTH','SUPPORT_TO_RESISTANCE','RESISTANCE_TO_SUPPORT'),
            PREFIX+'flip_direction','BOTH',lambda x:'Ambos' if x=='BOTH' else FLIP_LABELS[x])
        with b:reaction=_safe_select(st,'Reacción opuesta ya conocida',('M15_OR_H1','H1','ANY'),PREFIX+'flip_reaction','M15_OR_H1',
            lambda x:{'M15_OR_H1':'Registrada en 15m o 1h','H1':'Registrada en 1h','ANY':'Incluir candidatos pendientes'}[x])
        unique=st.checkbox('Primera llegada elegible por banda',key=PREFIX+'flip_unique',value=True)
        st.caption('Criterio descriptivo: defensas del rol anterior, ruptura opuesta confirmada en 1h y regreso desde el nuevo lado. '
                   'El cruce se contrasta con la secuencia de cierres 1h; reiniciar un episodio no crea una ruptura nueva. '
                   'Se excluyen rupturas ya fallidas o recuperadas y evidencia horaria incompleta. '
                   'La separación se mide con cierres 1h y límites conocidos en cada cierre. '
                   'El filtro usa registros V1.3; no constituye un censo continuo de todas las defensas posibles ni una tasa de éxito. '
                   'Varias visitas de una banda o un mismo movimiento de BTC pueden estar relacionadas.')
    args=dict(min_age_hours=age,min_defenses=defenses,min_separation_pct=distance,direction=direction,reaction=reaction)
    eligible=filter_flip_catalog(catalog,**args,unique_bands=False)
    cases=filter_flip_catalog(catalog,**args,unique_bands=unique)
    st.caption(f'{len(eligible)} llegadas elegibles · {eligible.region_id.nunique()} bandas · {len(cases)} casos en el selector. '
               f'Filtro: ≥{age} h de historia, ≥{defenses} defensas 1h, alejamiento ≥{distance:g}%. '
               'Todos los criterios se conocen antes de la llegada; POST permanece oculto.')
    if cases.empty:
        st.info('No hay casos con estos criterios. Podés reducir los umbrales o incluir candidatos pendientes.')
        return None,(age,defenses,distance,direction,reaction,unique)
    rows=cases.set_index('event_id').to_dict('index')
    case_id=_safe_select(st,'Caso estructural PRE',cases.event_id,PREFIX+'flip_case',format_func=lambda x:
        utc(rows[x]['pre_cutoff_ts'])+' · '+FLIP_LABELS[rows[x]['flip_direction']]+
        f" · {rows[x]['band_age_hours']/24:.1f} días · {rows[x]['prior_h1_defenses']} defensas 1h · "+
        {'H1':'reacción 1h','M15':'reacción 15m','PENDING':'pendiente'}[rows[x]['reaction_pre']])
    case={**rows[case_id],'event_id':case_id}
    with st.expander('Por qué aparece este caso · evidencia disponible en PRE'):
        values=[('Referencia de llegada',utc(case['pre_cutoff_ts'])),('Cambio de función en estudio',FLIP_LABELS[case['flip_direction']]),
                ('Banda conocida desde',utc(case['first_known_ts'])),('Antigüedad en la llegada',f"{case['band_age_hours']/24:.2f} días"),
                ('Defensas 1h del rol anterior',int(case['prior_h1_defenses'])),('Ruptura opuesta 1h',utc(case['break_ts'])),
                ('Máximo alejamiento cerrado conocido',f"{case['max_separation_pct']:.3f}% · {case['max_separation_widths']:.2f} anchos de banda"),
                ('Reacción opuesta',REACTION_LABELS[case['reaction_pre']]),('Posición PRE del último 1h cerrado',case['pre_h1_position']),
                ('Posición PRE del último 4h cerrado',case['pre_h4_position']),
                ('Retorno 4h sobre 12h, conocido en PRE',f"{case['pre_h4_return_12h_pct']:.3f}%"),
                ('Límites cambiaron desde la ruptura','Sí' if case['geometry_changed_since_break'] else 'No')]
        st.dataframe(pd.DataFrame([(name,str(value)) for name,value in values],columns=['Criterio','Valor']),**width)
        st.caption('Una reacción 15m no confirma automáticamente el cambio horario. Los retests sostenidos conservan la regla V1.3. '
                   'Los criterios de selección se fijan en la llegada; las transiciones de este modo avanzan desde esa referencia.')
    return case,(age,defenses,distance,direction,reaction,unique)


def stage_at(stages, region, cutoff):
    rows=stages.loc[stages.region_id.eq(region)&stages.known_from.le(int(cutoff))]
    if rows.empty:return None
    return rows.sort_values(['known_from','independent_extrema'],kind='mergesort').iloc[-1]


def price_position(price, stage):
    if stage is None:return 'UNKNOWN'
    return 'BELOW' if price<float(stage.band_low) else 'ABOVE' if price>float(stage.band_high) else 'INSIDE'


def visible_end(cutoff, source_end, reveal, post_minutes):
    if not reveal:return int(cutoff)
    if int(post_minutes)<=0:raise ValueError('Horizonte POST inválido')
    return min(int(source_end),int(cutoff)+int(post_minutes)*M)


def slice_bars(bars, start, end):
    # Filter by CLOSE, never by OPEN: no partially open 1h/4h in PRE.
    return bars.loc[bars.closed_ts.gt(int(start))&bars.closed_ts.le(int(end))].copy()


def visible_events(events, region, start, end, tf=None, episode=None, details=False):
    mask=events.region_id.eq(region)&events.confirmation_ts.gt(int(start))&events.confirmation_ts.le(int(end))
    if tf is not None:
        name={15:'M15_CLOSED',60:'H1_CLOSED'}.get(tf)
        if name is None:return events.iloc[0:0].copy()
        mask &= events.source_tf.eq(name)
    if episode is not None:mask &= events.episode_id.eq(episode)
    result=events.loc[mask].copy()
    if not details:result=result.loc[~result.event_type.str.startswith(('CLOSE_','DEPART_'))]
    return (result.sort_values(['confirmation_ts','asof_transition_id'],kind='mergesort')
            .drop_duplicates('asof_physical_transition_id').copy())


def visible_links(links, episode, end):
    h=links.h1_confirmation_ts.le(int(end))
    m=links.m15_confirmation_ts.isna()|links.m15_confirmation_ts.le(int(end))
    return links.loc[links.episode_id.eq(episode)&h&m].copy()


def visible_comparison(frame, episode, end):
    return frame.loc[frame.episode_id.eq(episode)&frame.confirmation_ts.le(int(end))].copy()


def blind_navigation(state, nav):
    """Must run BEFORE instantiating the POST checkbox on this rerun."""
    token=tuple(nav)
    changed=state.get(PREFIX+'navigation')!=token
    if changed:state[POST_KEY]=False
    state[PREFIX+'navigation']=token
    return changed


def cutoff_for(mode, episode, visit=None, transition=None, transition_cut='BEFORE_CANDLE'):
    if mode=='EPISODE':return int(episode['pre_cutoff_ts'])
    if mode=='VISIT':return int(visit['pre_cutoff_ts'])
    if mode=='TRANSITION':
        return int(transition['bar_open_ts'] if transition_cut=='BEFORE_CANDLE' else transition['confirmation_ts'])
    raise ValueError('Modo de corte inválido')


def event_label(kind, direction=''):
    side={'ABOVE':'arriba','BELOW':'debajo','INSIDE':'dentro'}.get(str(direction),'')
    if kind.startswith('BREAK_'):return 'Cruce '+side+' · 2 cierres'
    if kind.startswith('EXIT_'):return 'Salida '+side+' · 2 cierres'
    if kind=='DEFEND_ORIGIN_2C':return 'Defensa desde '+side
    if kind.startswith('FAILED_BREAK_'):return 'Ruptura fallida · retorno al origen'
    if kind.startswith('FAILED_EXIT_'):return 'Salida fallida · retorno dentro'
    if kind=='RETEST_TOUCH':return 'Retest · contacto de rango'
    if kind.startswith('RETEST_HOLD_'):return 'Cierres sostienen '+side
    if kind.startswith('REENTER_ORIGIN_'):return 'Primer regreso al lado de origen'
    if kind.startswith('REENTER_BAND_'):return 'Primer reingreso a la banda'
    if kind=='ACCEPT_INSIDE_3C':return 'Aceptación dentro · 3 cierres'
    if kind.startswith('CLOSE_'):return 'Cambio de cierre: '+side
    if kind.startswith('DEPART_'):return 'Alejamiento '+side+' · 2 cierres'
    return kind


def confirmation_family(kind):
    if kind.startswith(('FAILED_BREAK_', 'FAILED_EXIT_')):return 'FAILURE'
    if kind.startswith(('BREAK_', 'EXIT_')):return 'CROSS'
    if kind.startswith('DEFEND_'):return 'DEFENSE'
    if kind.startswith('RETEST_HOLD_'):return 'HOLD'
    if kind == 'RETEST_TOUCH':return 'RETEST'
    if kind.startswith('REENTER_'):return 'REENTRY'
    if kind == 'ACCEPT_INSIDE_3C':return 'ACCEPTANCE'
    if kind.startswith('DEPART_'):return 'DEPARTURE'
    return ''


def confirmation_label(kind, direction=''):
    if kind == 'DEFEND_ORIGIN_2C':
        return {'BELOW':'Rechazo desde abajo', 'ABOVE':'Defensa desde arriba'}.get(str(direction),event_label(kind,direction))
    if kind.startswith('RETEST_HOLD_'):
        side={'ABOVE':'arriba','BELOW':'debajo'}.get(str(direction),'')
        return 'Retest sostenido '+side+' · regla V1.3'
    if kind == 'RETEST_TOUCH':return 'Retest observado · contacto de rango'
    return event_label(kind,direction)


def transition_candidates(events, episode, follow_region=True):
    """Retrospective clock navigation, with no outcome labels in its selector."""
    mask=events.region_id.eq(episode['region_id'])
    mask &= events.confirmation_ts.gt(int(episode['pre_cutoff_ts']))
    mask &= events.confirmation_ts.le(int(episode['observed_until_ts']))
    if not follow_region:mask &= events.episode_id.eq(episode['episode_id'])
    return events.loc[mask].sort_values(['confirmation_ts','source_tf','asof_transition_id'],kind='mergesort').copy()


def arrival_confirmations(events, *, region, arrival, end, selected_episode, follow_region=True, tf=None):
    """Read existing V1.3 events only; do not infer missing crosses or holds.

    Region follow-up is a temporal association, not a rewrite of the selected
    episode's state machine. Preserve parent aliases before physical dedup so
    a retest/hold cannot be attached to a different cross or direction.
    """
    mask=events.region_id.eq(region)&events.confirmation_ts.gt(int(arrival))&events.confirmation_ts.le(int(end))
    if not follow_region:mask &= events.episode_id.eq(selected_episode)
    if tf is not None:mask &= events.source_tf.eq({15:'M15_CLOSED',60:'H1_CLOSED'}[tf])
    result=events.loc[mask].copy()
    result['confirmation_family']=result.event_type.map(confirmation_family)
    result=result.loc[result.confirmation_family.ne('')].copy()
    # Fail closed for missing evidence metadata. Range contact is observable,
    # while OHLC_INTRABAR_SEQUENCE_UNKNOWN still does not prove intrabar order.
    complete=result['data_complete'].fillna(False).eq(True) if 'data_complete' in result else pd.Series(False,index=result.index)
    result['confirmation_evaluable']=complete & result.stage_known_from.le(result.confirmation_ts)
    for field in ('confirmation_close','band_low_asof','band_high_asof'):
        result['confirmation_evaluable'] &= np.isfinite(pd.to_numeric(result[field],errors='coerce'))
    result['confirmation_evaluable'] &= result.band_low_asof.le(result.band_high_asof)
    aliases=result.groupby('asof_physical_transition_id',sort=False).asof_transition_id.agg(tuple).to_dict()
    result['_transition_aliases']=result.asof_physical_transition_id.map(aliases)
    result['_selected_context']=result.episode_id.eq(selected_episode)
    result=result.sort_values(['confirmation_ts','confirmation_evaluable','_selected_context','asof_transition_id'],
                              ascending=[True,False,False,True],kind='mergesort')
    return result.drop_duplicates('asof_physical_transition_id').copy()


def confirmation_track(frame, tf):
    rows=frame.loc[frame.source_tf.eq({15:'M15_CLOSED',60:'H1_CLOSED'}[tf])&frame.confirmation_evaluable].sort_values('confirmation_ts')
    def last(q):return None if q.empty else q.iloc[-1]
    cross=last(rows.loc[rows.confirmation_family.eq('CROSS')])
    defense=last(rows.loc[rows.confirmation_family.eq('DEFENSE')])
    retest=hold=failure=reentry=None
    headline='Sin confirmación posterior visible'
    if cross is not None:
        if 'parent_asof_transition_id' in rows:
            children=rows.loc[rows.parent_asof_transition_id.isin(cross['_transition_aliases'])
                              & rows.confirmation_ts.ge(int(cross.confirmation_ts))]
        else:children=rows.iloc[0:0]
        retest=last(children.loc[children.confirmation_family.eq('RETEST')])
        hold=last(children.loc[children.confirmation_family.eq('HOLD')])
        failure=last(children.loc[children.confirmation_family.eq('FAILURE')])
        reentry=last(children.loc[children.confirmation_family.eq('REENTRY')])
        headline='Cruce / salida confirmados'
        latest=last(children.loc[children.confirmation_family.isin(['RETEST','HOLD','FAILURE','REENTRY'])])
        if latest is not None:
            headline={'RETEST':'Retest observado · sostén pendiente', 'HOLD':'Retest sostenido · regla V1.3',
                      'FAILURE':'Cruce / salida fallidos','REENTRY':'Reingreso observado'}[latest.confirmation_family]
    elif defense is not None:headline=confirmation_label(defense.event_type,defense.direction)
    elif len(rows):headline=confirmation_label(rows.iloc[-1].event_type,rows.iloc[-1].direction)
    elif len(frame.loc[frame.source_tf.eq({15:'M15_CLOSED',60:'H1_CLOSED'}[tf])]):headline='Evidencia incompleta · sin confirmar'
    return dict(headline=headline,cross=cross,defense=defense,retest=retest,hold=hold,failure=failure,reentry=reentry)


def arrival_links(links, *, region, arrival, end, selected_episode, follow_region=True):
    mask=links.region_id.eq(region)&links.h1_confirmation_ts.gt(int(arrival))&links.h1_confirmation_ts.le(int(end))
    mask &= links.m15_confirmation_ts.gt(int(arrival))&links.m15_confirmation_ts.le(int(end))
    mask &= links.h1_confirmation_ts.ge(links.m15_confirmation_ts)
    mask &= links.link_status.isin(['LAGGED_CONFIRMATION','SAME_CLOSE'])
    if not follow_region:mask &= links.episode_id.eq(selected_episode)
    return links.loc[mask].sort_values('h1_confirmation_ts').copy()


def confirmation_table(frame, arrival, selected_episode):
    rows=[]
    for r in frame.itertuples(index=False):
        direction='' if pd.isna(r.direction) else str(r.direction)
        rows.append({'Confirmación UTC':utc(r.confirmation_ts),'TF':'15m' if r.source_tf=='M15_CLOSED' else '1h',
            'Evento':confirmation_label(r.event_type,direction),
            'Minutos desde la visita':round((int(r.confirmation_ts)-int(arrival))/M,1),
            'Contexto':'Episodio seleccionado' if r.episode_id==selected_episode else 'Otro episodio de la misma banda',
            'Evidencia':'Incompleta' if not r.confirmation_evaluable else
                        'Contacto por rango; orden intraminuto desconocido' if r.evidence_status=='OHLC_INTRABAR_SEQUENCE_UNKNOWN' else 'Cierres confirmados',
            'ID de evento':r.asof_transition_id,'ID físico':r.asof_physical_transition_id,
            'ID de episodio':r.episode_id,
            'ID de cruce asociado':getattr(r,'parent_asof_transition_id','')})
    return pd.DataFrame(rows)


def render_arrival_confirmations(st, frame, arrival, selected_episode, follow_region, width):
    st.markdown('##### Seguimiento de la llegada · confirmaciones conocidas')
    st.caption('Referencia de visita: '+utc(arrival)+' · '+
               ('Sigue la misma banda, incluidos otros episodios.' if follow_region else 'Solo el episodio seleccionado.')+
               ' Los horarios indican disponibilidad; una confirmación puede fallar después.')
    for col,tf in zip(st.columns(2),(15,60)):
        track=confirmation_track(frame,tf)
        with col:
            st.markdown('**'+('15m' if tf==15 else '1h')+' · '+track['headline']+'**')
            for field,label in [('defense','Defensa / rechazo'),('cross','Cruce / salida'),('retest','Retest'),
                                ('hold','Sostén registrado'),('failure','Fallo'),('reentry','Primer reingreso')]:
                event=track[field]
                if event is not None:
                    other=' · otro episodio' if event.episode_id!=selected_episode else ''
                    st.caption(label+': '+utc(event.confirmation_ts)+' · '+confirmation_label(event.event_type,event.direction)+other)
    shown=confirmation_table(frame,arrival,selected_episode)
    with st.expander('Secuencia y evidencia de las confirmaciones'):
        if shown.empty:st.caption('Todavía no hay eventos posteriores a la visita dentro del tramo visible.')
        else:
            st.dataframe(shown.drop(columns=['ID de evento','ID físico','ID de episodio','ID de cruce asociado']),**width)
            st.download_button('Descargar confirmaciones visibles',shown.to_csv(index=False).encode('utf-8-sig'),
                file_name='multiswing_v13_confirmaciones_visibles.csv',mime='text/csv',key=PREFIX+'download_confirmations')
        st.caption('Se muestran etiquetas ya calculadas por V1.3. Salida significa que el episodio comenzó dentro; '
                   'cruce parte de un lado conocido. El retest por rango no resuelve el orden intraminuto. '
                   'El sostén V1.3 conserva la racha global de cierres: no exige dos cierres nuevos después del retest. '
                   'Seguir otro episodio es una asociación temporal de la misma banda; no reescribe la secuencia del episodio original.')


def band_points(stages, region, start, end):
    known=stages.loc[stages.region_id.eq(region)&stages.known_from.le(int(end))]
    known=known.sort_values(['known_from','independent_extrema'],kind='mergesort').drop_duplicates('known_from',keep='last')
    old=known.loc[known.known_from.le(start)].tail(1)
    chosen=pd.concat([old,known.loc[known.known_from.gt(start)]])
    if chosen.empty:return [],[],[]
    t=[max(int(start),int(x)) for x in chosen.known_from]
    lo,hi=chosen.band_low.astype(float).tolist(),chosen.band_high.astype(float).tolist()
    if t[-1]<end:t.append(int(end));lo.append(lo[-1]);hi.append(hi[-1])
    return iso_values(t),lo,hi


def snapshot_table(bars, stages, region, cutoff):
    s=stage_at(stages,region,cutoff);rows=[]
    for tf,label in [(15,'15m'),(60,'1h'),(240,'4h')]:
        q=bars[tf].loc[bars[tf].closed_ts.le(int(cutoff))].tail(1)
        rows.append({'Temporalidad':label,'Último cierre UTC':utc(q.closed_ts.iloc[0]) if len(q) else 'Sin cierre',
                     'Precio':float(q.close.iloc[0]) if len(q) else None,
                     'Posición con la etapa conocida al corte':price_position(float(q.close.iloc[0]),s) if len(q) else 'UNKNOWN'})
    return pd.DataFrame(rows)


def build_figure(bars, stages, events, *, region, episode, cutoff, end, tf_minutes,
                 lookback_minutes, origin_side, compare_fixed=False, details=False,
                 selected_transition_id=None, followup_start=None, follow_region=True):
    start=int(cutoff)-int(lookback_minutes)*M
    candles=slice_bars(bars,start,end)
    chart_events=events
    if followup_start is not None and not follow_region:
        chart_events=events.loc[events.confirmation_ts.le(int(followup_start))|events.episode_id.eq(episode['episode_id'])]
    visible=visible_events(chart_events,region,start,end,tf_minutes,details=details)
    color='#48c7aa' if origin_side=='LOW' else '#e3af59'
    fill='rgba(72,199,170,.17)' if origin_side=='LOW' else 'rgba(227,175,89,.17)'
    bx,lo,hi=band_points(stages,region,start,end)
    data=[]
    if bx:
        data.extend([{'type':'scatter','x':bx,'y':lo,'mode':'lines','line':{'color':color,'width':1,'shape':'hv'},
                      'name':'Etapa confirmada as-of','legendgroup':'band','showlegend':False,'hoverinfo':'skip'},
                     {'type':'scatter','x':bx,'y':hi,'mode':'lines','line':{'color':color,'width':1,'shape':'hv'},
                      'fill':'tonexty','fillcolor':fill,'name':'Etapa confirmada as-of','legendgroup':'band','hoverinfo':'skip'}])
    if compare_fixed:
        t=max(start,int(episode['pre_cutoff_ts']))
        if t<=end:
            for key in ('stage_low','stage_high'):
                data.append({'type':'scatter','x':iso_values([t,end]),'y':[float(episode[key])]*2,
                             'mode':'lines','line':{'color':'#b8c3d8','width':1,'dash':'dot'},
                             'name':'Referencia fija del episodio','legendgroup':'fixed','showlegend':key=='stage_low','hoverinfo':'skip'})
    data.append({'type':'candlestick','x':iso_values(candles.closed_ts.to_numpy()),
                 'open':candles.open.astype(float).tolist(),'high':candles.high.astype(float).tolist(),
                 'low':candles.low.astype(float).tolist(),'close':candles.close.astype(float).tolist(),
                 'increasing':{'line':{'color':'#45bc9b'},'fillcolor':'#45bc9b'},
                 'decreasing':{'line':{'color':'#ee7477'},'fillcolor':'#ee7477'},
                 'name':{15:'15m',60:'1h',240:'4h'}[tf_minutes],'showlegend':False})
    if len(visible):
        text=[]
        for r in visible.itertuples(index=False):
            direction='' if pd.isna(r.direction) else str(r.direction)
            text.append(event_label(r.event_type,direction)+'<br>'+utc(r.confirmation_ts)+
                        '<br>Etapa disponible: '+utc(r.stage_known_from)+
                        ('<br>Orden intraminuto sin resolver' if r.evidence_status=='OHLC_INTRABAR_SEQUENCE_UNKNOWN' else ''))
        data.append({'type':'scatter','x':iso_values(visible.confirmation_ts.to_numpy()),
                     'y':visible.confirmation_close.astype(float).tolist(),'mode':'markers','name':'Eventos confirmados',
                     'marker':{'size':8,'color':'#a7d7ff','line':{'color':'#0c1320','width':1}},
                     'text':text,'hovertemplate':'%{text}<br>Precio: %{y:.2f}<extra></extra>'})
    follow=None
    if followup_start is not None and tf_minutes in (15,60):
        follow=arrival_confirmations(events,region=region,arrival=max(start,int(followup_start)),end=end,
                                     selected_episode=str(episode['episode_id']),follow_region=follow_region,tf=tf_minutes)
        follow=follow.loc[follow.confirmation_evaluable].copy()
        styles={'CROSS':('Cruce / salida','triangle-up','#ffd166'),
                'DEFENSE':('Defensa / rechazo','diamond','#bba7ff'),
                'RETEST':('Retest','circle-open','#f0bb6a'),
                'HOLD':('Sostén V1.3','square','#63d9b0'),
                'FAILURE':('Fallo','x','#f17e86')}
        for family,(name,symbol,tint) in styles.items():
            q=follow.loc[follow.confirmation_family.eq(family)]
            if q.empty:continue
            symbols=['triangle-down' if family=='CROSS' and r.direction=='BELOW' else symbol for r in q.itertuples(index=False)]
            hover=[confirmation_label(r.event_type,r.direction)+'<br>'+utc(r.confirmation_ts)+
                   ('<br>Otro episodio de la misma banda' if r.episode_id!=episode['episode_id'] else '<br>Episodio seleccionado')+
                   ('<br>Orden intraminuto sin resolver' if r.evidence_status=='OHLC_INTRABAR_SEQUENCE_UNKNOWN' else '')
                   for r in q.itertuples(index=False)]
            data.append({'type':'scatter','x':iso_values(q.confirmation_ts.to_numpy()),
                         'y':q.confirmation_close.astype(float).tolist(),'mode':'markers','name':name,
                         'marker':{'size':10,'symbol':symbols,'color':tint,'line':{'width':1}},
                         'text':hover,'hovertemplate':'%{text}<br>Precio: %{y:.2f}<extra></extra>'})
    annotations=[]
    # Filter the chosen episode BEFORE physical deduplication. The same event
    # can belong to several episodes; a representative from another episode
    # must not make the selected transition disappear from navigation.
    selected_events=visible_events(events,region,start,end,tf_minutes,episode=str(episode['episode_id']),details=True)
    focus=selected_events
    if follow is not None:
        # Follow the region after the chosen visit, even if V1.3 recorded a
        # later cross in a different episode. Keep episode-only PRE history.
        focus=pd.concat([selected_events.loc[selected_events.confirmation_ts.le(int(followup_start))],follow])
    focus=focus.loc[focus.event_type.str.startswith(('BREAK_','EXIT_','FAILED_','DEFEND_','RETEST_HOLD_'))|focus.event_type.eq('RETEST_TOUCH')]
    focus=focus.sort_values('confirmation_ts').drop_duplicates('event_type',keep='last').tail(7 if follow is not None else 5)
    exact=events.loc[events.asof_transition_id.eq(selected_transition_id)] if selected_transition_id else events.iloc[0:0]
    if not follow_region:exact=exact.loc[exact.episode_id.eq(episode['episode_id'])]
    selected=visible_events(exact,region,start,end,tf_minutes,details=True)
    if len(selected):
        focus=pd.concat([focus,selected]).drop_duplicates('asof_transition_id')
        data.append({'type':'scatter','x':iso_values(selected.confirmation_ts.to_numpy()),
                     'y':selected.confirmation_close.astype(float).tolist(),'mode':'markers',
                     'marker':{'size':15,'symbol':'circle-open','color':'#ffd166','line':{'width':2}},
                     'name':'Transición elegida','hovertemplate':'Transición elegida<br>%{x}<br>%{y:.2f}<extra></extra>'})
    for i,r in enumerate(focus.itertuples(index=False)):
        fraction=(int(r.confirmation_ts)-start)/max(1,end-start)
        right=fraction>.5
        labeler=confirmation_label if follow is not None and int(r.confirmation_ts)>int(followup_start) else event_label
        label='<br>'.join(textwrap.wrap(labeler(r.event_type,'' if pd.isna(r.direction) else str(r.direction)),28))
        annotations.append({'x':iso_values([r.confirmation_ts])[0],'y':float(r.confirmation_close),
            'text':label,'xanchor':'right' if right else 'left',
            'showarrow':True,'arrowhead':0,'ax':-28 if right else 28,'ay':-42 if i%2==0 else 44,
            'bgcolor':'rgba(12,19,32,.92)','bordercolor':'#36536f','font':{'size':10,'color':'#e8edf6'},
            'arrowcolor':'#99bbda'})
    cut=iso_values([cutoff])[0]
    shapes=[{'type':'line','xref':'x','yref':'paper','x0':cut,'x1':cut,'y0':0,'y1':1,
             'line':{'color':'#f6cb64','width':1.4,'dash':'dash'}}]
    if end>cutoff:
        shapes.insert(0,{'type':'rect','xref':'x','yref':'paper','x0':cut,'x1':iso_values([end])[0],
                         'y0':0,'y1':1,'line':{'width':0},'fillcolor':'rgba(125,153,205,.09)','layer':'below'})
    annotations.append({'x':cut,'y':1,'yref':'paper','text':'Corte PRE','showarrow':False,
                        'xanchor':'right','yanchor':'top','font':{'size':10,'color':'#f6cb64'}})
    label={15:'15m · interacción local',60:'1h · estructura e historia',240:'4h · contexto cerrado'}[tf_minutes]
    layout={'title':{'text':label,'x':.01},'height':430,'paper_bgcolor':'#0c1320','plot_bgcolor':'#121d2e',
            'font':{'color':'#e6edf7','size':11},'margin':{'l':60,'r':25,'t':55,'b':45},
            'xaxis':{'type':'date','range':iso_values([start,end]),'rangeslider':{'visible':False},
                     'gridcolor':'#27354a','title':{'text':'UTC · hora de cierre'}},
            'yaxis':{'title':{'text':'USD'},'gridcolor':'#27354a','fixedrange':False},
            'hovermode':'closest','legend':{'orientation':'h','y':1.04,'x':.15},
            'shapes':shapes,'annotations':annotations,
            'uirevision':f'{region}|{episode["episode_id"]}|{cutoff}|{end}|{tf_minutes}|{lookback_minutes}'}
    return {'data':data,'layout':layout}


def timeline_table(frame, cutoff):
    rows=[]
    for r in frame.itertuples(index=False):
        direction='' if pd.isna(r.direction) else str(r.direction)
        rows.append({'Hora UTC':utc(r.confirmation_ts),'TF':'15m' if r.source_tf=='M15_CLOSED' else '1h',
                     'Evento':event_label(r.event_type,direction),'Lado':direction or '—',
                     'Tramo':'PRE' if int(r.confirmation_ts)<=cutoff else 'POST',
                     'Precio de confirmación':float(r.confirmation_close),
                     'Límite inferior':float(r.band_low_asof),'Límite superior':float(r.band_high_asof)})
    return pd.DataFrame(rows)


def _safe_select(st,label,options,key,default=None,format_func=None):
    options=list(options)
    if not options:return None
    state=st.session_state
    if state.get(key) not in options:state[key]=default if default in options else options[0]
    kwargs={'key':key}
    if format_func is not None:kwargs['format_func']=format_func
    return st.selectbox(label,options,**kwargs)


def width_options(st):
    # Keep the VPS's older Streamlit API supported while avoiding deprecation
    # warnings on newer releases. Dataframe width used to accept integers only.
    try:
        version=tuple(int(x) for x in st.__version__.split('.')[:2])
    except (AttributeError,ValueError):version=(0,0)
    return {'width':'stretch'} if version>=(1,51) else {'use_container_width':True}


def render_multiswing_behavior_v13(project, st=None):
    if st is None:
        import streamlit as st
    started=time.perf_counter()
    root=Path(project).resolve()
    st.markdown('#### Episodios estructurales · V1.3')
    try:
        study=load_index(root)
    except FileNotFoundError:
        st.info('El run V1.3 todavía no está disponible. Esperá MS_BEHAVIOR_V13_FINISHED y actualizá la lectura.')
        if st.button('Actualizar lectura',key=PREFIX+'refresh_missing'):
            clear_caches();st.rerun()
        return {'status':'RUN_NOT_AVAILABLE'}
    except (ValueError,KeyError,OSError) as exc:
        st.error('No se pudo verificar el estudio V1.3: '+str(exc))
        return {'status':'INVALID_STUDY'}
    state=st.session_state
    width=width_options(st)
    presets={'LOW_23_OCT':'LOW · 23 octubre · visita 29',
             'HIGH_22_OCT':'HIGH · 22 octubre · visita 19',
             'HIGH_18_SEP':'HIGH · 18 septiembre · visita 1',
             'FLIPS_PRE':'Flips con historia · selección estructural PRE', 'CATALOG':'Explorar todos los episodios BTC'}
    preset=_safe_select(st,'Punto de partida',PRESETS,PREFIX+'preset','LOW_23_OCT',presets.get)
    changed_preset=state.get(PREFIX+'last_preset')!=preset
    if changed_preset:
        state[POST_KEY]=False
        if preset in BOOKMARKS:
            r,e,v=BOOKMARKS[preset]
            state[PREFIX+'region']=r;state[PREFIX+'episode']=e;state[PREFIX+'visit']=v
            state[PREFIX+'anchor']='VISIT'
        state[PREFIX+'last_preset']=preset
    flip_case=None;flip_filters=()
    eps=study['episodes']
    if preset in BOOKMARKS:eps=eps.loc[eps.region_id.eq(BOOKMARKS[preset][0])]
    elif preset=='FLIPS_PRE':
        try:flip_case,flip_filters=render_flip_selector(st,root,width)
        except (ValueError,KeyError,OSError) as exc:
            st.error('No se pudo construir el catálogo de flips PRE: '+str(exc));return {'status':'INVALID_FLIP_CATALOG'}
        if flip_case is None:
            state[POST_KEY]=False
            return {'status':'NO_FLIP_CASES'}
        if changed_preset or state.get(PREFIX+'last_flip_case')!=flip_case['event_id']:
            state[POST_KEY]=False
            state[PREFIX+'region']=flip_case['region_id'];state[PREFIX+'episode']=flip_case['episode_id']
            state[PREFIX+'visit']=flip_case['event_id'];state[PREFIX+'anchor']='VISIT'
        state[PREFIX+'last_flip_case']=flip_case['event_id']
        eps=eps.loc[eps.episode_id.eq(flip_case['episode_id'])]
    else:
        side=_safe_select(st,'Origen de la banda',('BOTH','LOW','HIGH'),PREFIX+'side','BOTH',
                          lambda x:{'BOTH':'Ambos','LOW':'LOW','HIGH':'HIGH'}[x])
        if side!='BOTH':eps=eps.loc[eps.band_origin_side.eq(side)]
    if eps.empty:
        st.info('No hay episodios bajo este filtro.');return {'status':'NO_EPISODES'}
    regions=sorted(eps.region_id.unique())
    sides=eps.drop_duplicates('region_id').set_index('region_id').band_origin_side.to_dict()
    band_col,episode_col=st.columns(2)
    with band_col:
        region=_safe_select(st,'Banda',regions,PREFIX+'region',format_func=lambda r:sides[r]+' · '+r)
    eps=eps.loc[eps.region_id.eq(region)].sort_values('pre_cutoff_ts')
    epmap=eps.set_index('episode_id').to_dict('index')
    with episode_col:
        eid=_safe_select(st,'Episodio',eps.episode_id,PREFIX+'episode',format_func=lambda x:'Desde '+utc(epmap[x]['pre_cutoff_ts']))
    ep={**epmap[eid],'episode_id':eid}
    nav=_safe_select(st,'Situar el corte PRE',('VISIT','TRANSITION') if flip_case is not None else ('VISIT','EPISODE','TRANSITION'),PREFIX+'anchor','VISIT',
        lambda x:{'VISIT':'Antes del minuto de una visita','EPISODE':'Inicio del episodio',
                  'TRANSITION':'Vela de una transición'}[x])
    follow_region=st.checkbox('Seguir la misma banda en otros episodios',key=PREFIX+'follow_region',value=True,
        help='Vincula por tiempo las confirmaciones de la región desde esta visita. Conserva el episodio de origen de cada evento.')
    visit=None;transition=None;tid=None;vid=None;transition_cut='BEFORE_CANDLE'
    if nav=='VISIT':
        visits=study['visits'].loc[study['visits'].episode_id.eq(eid)].sort_values('pre_cutoff_ts')
        if flip_case is not None:visits=visits.loc[visits.event_id.eq(flip_case['event_id'])]
        vmap=visits.set_index('event_id').to_dict('index')
        vid=_safe_select(st,'Visita',visits.event_id,PREFIX+'visit',format_func=lambda x:
                         f"Visita {int(vmap[x]['visit_number'])} · {utc(vmap[x]['pre_cutoff_ts'])} · desde {vmap[x]['approach_position']}")
        if vid is None:st.info('Este episodio no tiene visitas disponibles.');return {'status':'NO_VISITS'}
        visit=vmap[vid]
    elif nav=='TRANSITION':
        candidates=transition_candidates(study['events'],ep,follow_region)
        if flip_case is not None:candidates=candidates.loc[candidates.bar_open_ts.ge(int(flip_case['pre_cutoff_ts']))]
        emap=candidates.set_index('asof_transition_id').to_dict('index')
        numbers={x:i+1 for i,x in enumerate(candidates.asof_transition_id)}
        tid=_safe_select(st,'Vela de estudio',candidates.asof_transition_id,PREFIX+'transition',
            format_func=lambda x:f"#{numbers[x]} · {'15m' if emap[x]['source_tf']=='M15_CLOSED' else '1h'} · abre {utc(emap[x]['bar_open_ts'])}"+
                (' · otro episodio' if emap[x]['episode_id']!=eid else ''))
        if tid is None:st.info('Este episodio no tiene transiciones disponibles.');return {'status':'NO_TRANSITIONS'}
        transition=emap[tid]
        transition_cut=_safe_select(st,'Instante de la vela',('BEFORE_CANDLE','AT_CONFIRMATION'),PREFIX+'transition_cut','BEFORE_CANDLE',
            lambda x:{'BEFORE_CANDLE':'PRE antes de abrir la vela','AT_CONFIRMATION':'Al cierre confirmado'}[x])
        st.caption('La etiqueta de la transición aparece solo cuando su cierre queda incluido en el corte o se revela POST.')
    cutoff=cutoff_for(nav,ep,visit,transition,transition_cut)
    try:bars,source=load_prices(root,study)
    except (ValueError,FileNotFoundError,KeyError,OSError) as exc:
        st.error('No se pudo abrir la fuente congelada: '+str(exc));return {'status':'INVALID_PRICES'}
    c1,c2,c3=st.columns(3)
    with c1:local_hours=_safe_select(st,'Historia 15m · horas',(12,24,48,72),PREFIX+'local_hours',24)
    with c2:h1_days=_safe_select(st,'Historia 1h · días',(1,3,7,14,30),PREFIX+'h1_days',7)
    with c3:h4_days=_safe_select(st,'Contexto 4h · días',(1,3,7,14,30,60),PREFIX+'h4_days',14)
    blind_navigation(state,(preset,region,eid,nav,vid or '',tid or '',transition_cut,cutoff,local_hours,h1_days,h4_days,follow_region,flip_filters))
    post_col,horizon_col,compare_col=st.columns(3)
    with post_col:reveal=st.checkbox('Revelar POST',key=POST_KEY,value=False)
    with horizon_col:post_minutes=_safe_select(st,'POST · minutos',(15,30,60,180,360,720,1440),PREFIX+'post_minutes',720)
    end=visible_end(cutoff,source['end'],reveal,post_minutes)
    with compare_col:compare=st.checkbox('Comparar con la referencia fija',key=PREFIX+'compare',value=False)
    with st.expander('Opciones de la cronología'):
        details=st.checkbox('Incluir cambios de cierre y alejamientos',key=PREFIX+'details',value=False)
        history=st.checkbox('Incluir eventos previos de la región en la cronología',key=PREFIX+'history',value=True)
    st.caption('Corte común: '+utc(cutoff)+' · '+('POST revelado hasta '+utc(end) if reveal else 'POST oculto')+
               ' · Las tres vistas utilizan únicamente velas cerradas dentro de su tramo.')
    st.dataframe(snapshot_table(bars,study['stages'],region,cutoff),**width)
    known_stages=study['stages'].loc[study['stages'].region_id.eq(region)&study['stages'].known_from.le(cutoff)]
    if len(known_stages):
        first_known=int(known_stages.known_from.min())
        st.caption('Banda conocida desde '+utc(first_known)+f' · historia disponible en PRE: {(cutoff-first_known)/(60*M):.2f} h. '
                   'El tramo coloreado representa su historia confirmada dentro de la ventana.')
    if stage_at(study['stages'],region,cutoff) is None:
        st.info('Todavía no había una etapa geométrica confirmada en este corte; se conservan los precios cerrados sin atribuirles una banda futura.')
    if reveal and end<cutoff+post_minutes*M:
        st.caption('El POST está recortado por el final de la fuente congelada.')
    arrival=int(flip_case['pre_cutoff_ts']) if flip_case is not None else int(visit['pre_cutoff_ts']) if visit is not None else int(ep['pre_cutoff_ts'])
    confirmations=arrival_confirmations(study['events'],region=region,arrival=arrival,end=end,
                                       selected_episode=eid,follow_region=follow_region)
    render_arrival_confirmations(st,confirmations,arrival,eid,follow_region,width)
    for tf,minutes in [(15,local_hours*60),(60,h1_days*1440),(240,h4_days*1440)]:
        figure=build_figure(bars[tf],study['stages'],study['events'],region=region,episode=ep,
            cutoff=cutoff,end=end,tf_minutes=tf,lookback_minutes=minutes,origin_side=ep['band_origin_side'],
            compare_fixed=compare,details=details,selected_transition_id=tid,followup_start=arrival,follow_region=follow_region)
        st.plotly_chart(figure,**width,key=PREFIX+'chart_'+str(tf),
                        config={'displaylogo':False,'scrollZoom':True})
    st.markdown('##### Cronología conocida en el tramo visible')
    first=cutoff-max(local_hours*60,h1_days*1440)*M
    timeline=visible_events(study['events'],region,first,end,episode=None if history else eid,details=details)
    shown=timeline_table(timeline,cutoff)
    if shown.empty:st.info('No hay eventos confirmados en el tramo visible.')
    else:
        st.dataframe(shown,**width)
        st.download_button('Descargar cronología visible',shown.to_csv(index=False).encode('utf-8-sig'),
            file_name='multiswing_v13_cronologia_visible.csv',mime='text/csv',key=PREFIX+'download_timeline')
    with st.expander('Relación entre eventos 15m y 1h desde la visita'):
        links=arrival_links(study['links'],region=region,arrival=arrival,end=end,selected_episode=eid,follow_region=follow_region)
        display=[]
        for r in links.itertuples(index=False):
            display.append({'Evento':event_label(r.event_type,'' if pd.isna(r.direction) else str(r.direction)),
                'Cierre 15m UTC':utc(r.m15_confirmation_ts),'Cierre 1h UTC':utc(r.h1_confirmation_ts),
                'Retraso · minutos':int(r.lag_minutes),
                'Relación':'Mismo cierre' if r.link_status=='SAME_CLOSE' else 'Confirmación posterior',
                'Contexto':'Episodio seleccionado' if r.episode_id==eid else 'Otro episodio de la misma banda',
                'Cambian los límites entre confirmaciones':bool(r.geometry_changed_between_confirmations)})
        if display:st.dataframe(pd.DataFrame(display),**width)
        else:st.caption('No hay un vínculo confirmado visible desde esta visita.')
    if compare:
        try:
            audit=visible_comparison(load_comparison(root),eid,end)
            st.markdown('##### Referencia fija frente a geometría causal')
            display=pd.DataFrame({'Cierre UTC':audit.confirmation_ts.map(utc),'TF':audit.source_tf,
                'Etiqueta con referencia fija':audit.parent_event_type,'Etiqueta causal':audit.asof_event_type.fillna('—'),
                'Comparación':audit.comparison_status.map(STATUS_LABELS)})
            st.dataframe(display,**width)
        except (ValueError,FileNotFoundError,KeyError,OSError) as exc:
            st.warning('Comparación no disponible: '+str(exc))
    st.caption('La navegación recorre episodios históricos. 4h aporta contexto cerrado; los eventos de esta versión se calculan en 15m y 1h.')
    st.caption(f'Vista V1.3.2: {time.perf_counter()-started:.2f} s · datos calculados offline; sin reconstrucción de episodios al navegar.')
    return {'status':'OK','cutoff':cutoff,'visible_end':end,'post_revealed':bool(reveal),
            'episode_id':eid,'region_id':region,'visible_timeline_rows':len(shown),
            'visible_confirmation_rows':len(confirmations)}
