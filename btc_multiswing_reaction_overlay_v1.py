"""REACTION overlay for the BTC 60D inspector, without changing its detectors.

Historical mode reuses hll_reaction_pre_overlay's canonical 15m/3x3 replay on
the verified Multi-Swing minute freeze (including its original warmup).
Journal mode reads first_touches through SQLite mode=ro. Sources never mix.
Every marker/table/export is filtered by known_ts, the retest minute CLOSE.
Spatial comparison uses the original swing price and geometry known at the
retest OPEN. Proximity is descriptive, not physical identity or a trade signal.
"""
from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import sqlite3
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

M=60_000
VERSION='btc-multiswing-reaction-overlay-v1'
JOURNAL=Path('reports/reaction_lab/scanner_first_touches_v1.sqlite3')
HLL_MODULE=Path('dashboard/views/hll_reaction_pre_overlay.py')
SOURCES=('HLL_HISTORICAL','JOURNAL')
COLUMNS=('event_id','touch_key','symbol','side','status','retest_ts','known_ts',
         'swing_price','retest_close','source')


def utc(ts):
    return pd.to_datetime(int(ts),unit='ms',utc=True).strftime('%Y-%m-%d %H:%M UTC')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1<<20),b''):h.update(part)
    return h.hexdigest()


def _stat(path,optional=False):
    path=Path(path)
    if optional and not path.exists():return (str(path),None,None)
    s=path.stat();return (str(path),s.st_size,s.st_mtime_ns)


def _freeze_token(root):
    from btc_multiswing_60d_explorer_v1 import FREEZE_REL,CATALOG_REL
    return tuple(_stat(root/rel/name) for rel,names in [
        (FREEZE_REL,('manifest.json','BTCUSDT_1m.csv','BTCUSDT_1m_warmup.csv')),
        (CATALOG_REL,('manifest.json','confirmed_swings.csv'))] for name in names)


def validate_events(frame):
    if not set(COLUMNS).issubset(frame.columns):raise ValueError('Esquema REACTION incompleto')
    x=frame[list(COLUMNS)].copy()
    if x.empty:return x
    if x.event_id.isna().any() or x.event_id.astype(str).eq('').any() or x.event_id.duplicated().any():
        raise ValueError('Identidad REACTION vacía o duplicada')
    if not x.side.isin(['LONG','SHORT']).all() or x.symbol.ne('BTCUSDT').any() or x.status.ne('REACTION').any():
        raise ValueError('El overlay admite REACTIONs BTC LONG/SHORT originales')
    for col in ('retest_ts','known_ts'):
        values=pd.to_numeric(x[col],errors='raise')
        if not np.isfinite(values.to_numpy(dtype=float)).all() or (values%1).any():
            raise ValueError('Reloj REACTION inválido')
        x[col]=values.astype('int64')
    if (x.retest_ts%M).any() or x.known_ts.sub(x.retest_ts).ne(M).any():
        raise ValueError('REACTION no disponible al cierre de su minuto de retest')
    for col in ('swing_price','retest_close'):
        x[col]=pd.to_numeric(x[col],errors='raise')
        present=x[col].notna()
        if col=='retest_close' and not present.all():raise ValueError('Falta cierre de retest')
        if not np.isfinite(x.loc[present,col].to_numpy(dtype=float)).all() or x.loc[present,col].le(0).any():
            raise ValueError('Precio REACTION inválido')
    return x.sort_values(['known_ts','event_id'],kind='mergesort').reset_index(drop=True)


@lru_cache(maxsize=2)
def _hll_module(path_string,token):
    path=Path(path_string)
    name='_ms60_original_hll_'+hashlib.sha256(path_string.encode()).hexdigest()[:12]
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:raise ImportError('No se puede abrir el overlay original de HLL')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=2)
def _historical(root_string,token):
    from btc_multiswing_60d_explorer_v1 import load_snapshot
    root=Path(root_string);full,_,source=load_snapshot(root)
    hll=_hll_module(str(root/HLL_MODULE),_stat(root/HLL_MODULE))
    app=root/'dashboard/app.py'
    env,detector,app_sha=hll.canonical_functions(str(app),app.stat().st_mtime_ns)
    events=hll.replay_canonical_pre(full,int(source['end']),int(source['start']),
        env=env,swing_detector_class=detector,symbol='BTCUSDT')
    rows=[dict(event_id=r['event_key'],touch_key='',symbol=r['symbol'],side=r['side'],
        status=r['status'],retest_ts=r['retest_ts'],known_ts=r['known_ts'],
        swing_price=r['swing_price'],retest_close=r['retest_close'],
        source='HLL_HISTORICAL_CANONICAL_15M_3X3') for r in events]
    frame=validate_events(pd.DataFrame(rows,columns=COLUMNS))
    return frame,dict(mode='HLL_HISTORICAL',label='Histórico canónico 15m · igual que HLL',
        coverage_start=int(source['start']),coverage_end=int(source['end']),
        source_sha256=source['source_sha256'],canonical_app_sha256=app_sha,
        hll_overlay_sha256=sha(root/HLL_MODULE),
        detector_sha256=sha(root/'engine/live/research/swing_detector.py'))


def read_journal(path,minute,*,start,end):
    """Read only causal columns; never select future outcomes or alter a DB."""
    path=Path(path)
    if not path.is_file():raise FileNotFoundError('No está disponible el journal guardado de primeros retests')
    con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        con.execute('PRAGMA query_only=ON')
        names={r[1] for r in con.execute('PRAGMA table_info(first_touches)')}
        needed={'touch_key','symbol','side','status','retest_ts','known_ts','causal_json'}
        if not needed.issubset(names):raise ValueError('El journal no tiene el esquema canónico first_touches')
        rows=con.execute('SELECT touch_key,symbol,side,status,retest_ts,known_ts,causal_json '
            'FROM first_touches WHERE symbol=? AND status=? AND known_ts>=? AND known_ts<=? '
            'ORDER BY known_ts,touch_key',
            ('BTCUSDT','REACTION',int(start),int(end))).fetchall()
    finally:con.close()
    closes=minute.set_index('timestamp')['close'];out=[]
    for key,symbol,side,status,retest,known,raw in rows:
        try:payload=json.loads(raw) if raw else {}
        except (ValueError,TypeError) as exc:raise ValueError('JSON causal inválido en el journal') from exc
        if not isinstance(payload,dict):raise ValueError('JSON causal inválido en el journal')
        price=None
        for field in ('swing_price','pivot_price'):
            value=payload.get(field)
            if value is not None:
                try:value=float(value)
                except (ValueError,TypeError):raise ValueError('Precio de swing inválido en el journal')
                if not np.isfinite(value) or value<=0:raise ValueError('Precio de swing inválido en el journal')
                price=value;break
        if int(retest) not in closes.index:raise ValueError('Retest del journal fuera del freeze 1m')
        out.append(dict(event_id=str(key),touch_key=str(key),symbol=str(symbol),side=str(side),status=str(status),
            retest_ts=retest,known_ts=known,swing_price=price,retest_close=float(closes.loc[int(retest)]),
            source='CANONICAL_FIRST_TOUCH_JOURNAL_READONLY'))
    return validate_events(pd.DataFrame(out,columns=COLUMNS))


@lru_cache(maxsize=2)
def _journal(root_string,token):
    from btc_multiswing_60d_explorer_v1 import load_snapshot
    root=Path(root_string);minute,_,source=load_snapshot(root)
    frame=read_journal(root/JOURNAL,minute,start=int(source['start']),end=int(source['end']))
    return frame,dict(mode='JOURNAL',label='REACTIONs guardadas · journal original',
        coverage_start=int(source['start']),coverage_end=int(source['end']),
        source_sha256=source['source_sha256'],coverage_complete_not_assumed=True)


def load_reactions(project,mode='HLL_HISTORICAL'):
    root=Path(project).resolve();token=_freeze_token(root)
    if mode=='HLL_HISTORICAL':
        token+=tuple(_stat(root/p) for p in [HLL_MODULE,Path('dashboard/app.py'),Path('engine/live/research/swing_detector.py')])
        return _historical(str(root),token)
    if mode=='JOURNAL':
        path=root/JOURNAL
        if not path.is_file():raise FileNotFoundError('No está disponible el journal guardado de primeros retests')
        token+=(_stat(path),_stat(Path(str(path)+'-wal'),True))
        return _journal(str(root),token)
    raise ValueError('Fuente de REACTIONs desconocida')


def clear_caches():
    _historical.cache_clear();_journal.cache_clear();_hll_module.cache_clear()


def visible_reactions(frame,*,start,end,cutoff,sides=('LONG','SHORT')):
    if int(start)>int(end) or int(cutoff)>int(end):raise ValueError('Intervalo del overlay inválido')
    if not set(sides).issubset({'LONG','SHORT'}):raise ValueError('Sentido REACTION inválido')
    x=frame.loc[frame.known_ts.ge(int(start))&frame.known_ts.le(int(end))&frame.side.isin(sides)].copy()
    x['phase']=np.where(x.known_ts.le(int(cutoff)),'PRE','POST')
    return x.reset_index(drop=True)


def compare_bands(events,stages,*,region_ids=None,tolerance_pct=.10):
    """Compare original swing levels against stages known at retest OPEN."""
    if not 0<=float(tolerance_pct)<=2:raise ValueError('Cercanía fuera de banda inválida')
    required={'region_id','known_from','band_low','band_high'}
    if not required.issubset(stages.columns):raise ValueError('Falta geometría causal para comparar bandas')
    s=stages.loc[:,list(required)].copy()
    if region_ids is not None:s=s.loc[s.region_id.isin(set(region_ids))]
    for col in ('known_from','band_low','band_high'):s[col]=pd.to_numeric(s[col],errors='raise')
    if not s.empty and (not np.isfinite(s[['known_from','band_low','band_high']].to_numpy(dtype=float)).all()
        or s.band_low.le(0).any() or s.band_high.lt(s.band_low).any()):
        raise ValueError('Geometría de banda inválida')
    s=s.sort_values('known_from',kind='mergesort').drop_duplicates(['region_id','known_from'],keep='last')
    x=events.copy().reset_index(drop=True);n=len(x)
    best=np.full(n,np.inf);best_id=np.full(n,'',dtype=object)
    lows=np.full(n,np.nan);highs=np.full(n,np.nan);known=np.full(n,np.nan)
    matches=[[] for _ in range(n)]
    times=x.retest_ts.to_numpy(dtype='int64');prices=x.swing_price.to_numpy(dtype=float)
    for rid,g in s.groupby('region_id',sort=True):
        clocks=g.known_from.to_numpy(dtype='int64');idx=np.searchsorted(clocks,times,side='right')-1
        mask=(idx>=0)&np.isfinite(prices)
        at=np.flatnonzero(mask)
        if not len(at):continue
        bounds=g[['band_low','band_high']].to_numpy(dtype=float)[idx[at]]
        gaps=np.maximum.reduce([np.zeros(len(at)),bounds[:,0]-prices[at],prices[at]-bounds[:,1]])/bounds.mean(axis=1)*100
        improve=gaps<best[at];chosen=at[improve]
        best[chosen]=gaps[improve];best_id[chosen]=str(rid)
        lows[chosen]=bounds[improve,0];highs[chosen]=bounds[improve,1];known[chosen]=clocks[idx[chosen]]
        for row in at[gaps<=float(tolerance_pct)+1e-12]:matches[int(row)].append(str(rid))
    x['nearest_region_id']=best_id;x['band_low_at_retest']=lows;x['band_high_at_retest']=highs
    x['stage_known_from']=known;x['gap_to_band_pct']=np.where(np.isfinite(best),best,np.nan)
    x['matched_region_ids']=[','.join(v) for v in matches];x['matched_band_count']=[len(v) for v in matches]
    x['near_band']=x.matched_band_count.gt(0)
    x['match_status']=np.where(~np.isfinite(prices),'SWING_PRICE_UNAVAILABLE',
        np.where(~np.isfinite(best),'NO_BAND_KNOWN_AT_RETEST',np.where(best==0,'INSIDE_BAND',
        np.where(x.near_band,'NEAR_BAND','OUTSIDE_TOLERANCE'))))
    return x


def marker_traces(frame,*,start,end):
    x=frame.loc[frame.known_ts.ge(int(start))&frame.known_ts.le(int(end))]
    traces=[]
    for side,color,symbol in [('LONG','#35e2a3','triangle-up'),('SHORT','#ff7aa5','triangle-down')]:
        q=x.loc[x.side.eq(side)]
        if q.empty:continue
        text=[]
        for r in q.itertuples(index=False):
            level='No disponible' if pd.isna(r.swing_price) else f'{r.swing_price:,.2f}'
            relation={'INSIDE_BAND':'Nivel dentro de banda','NEAR_BAND':'Nivel cerca de banda',
                'OUTSIDE_TOLERANCE':'Nivel fuera de tolerancia','NO_BAND_KNOWN_AT_RETEST':'Sin banda conocida en ese retest',
                'SWING_PRICE_UNAVAILABLE':'Sin precio original para comparar'}[r.match_status]
            band=html.escape(str(r.nearest_region_id)) if r.nearest_region_id else '—'
            bounds='—' if pd.isna(r.band_low_at_retest) else f'{r.band_low_at_retest:,.2f}–{r.band_high_at_retest:,.2f}'
            text.append(f'<b>REACTION {side}</b> · {r.phase}<br>Conocida: {utc(r.known_ts)}'
                f'<br>Retest: {utc(r.retest_ts)}<br>Nivel swing: {level}<br>{relation}'
                f'<br>Banda al retest: {bounds}<br>Región: {band}'
                f'<br>Clave original: {html.escape(str(r.event_id))}')
        traces.append(dict(type='scattergl',x=pd.to_datetime(q.known_ts,unit='ms',utc=True).dt.strftime('%Y-%m-%dT%H:%M:%SZ').tolist(),
            y=q.retest_close.tolist(),mode='markers',name='REACTION '+side+' · 15m',legendgroup='reaction_'+side,
            marker=dict(color=color,size=12,symbol=symbol,line=dict(width=1.5,color='#edf2ff')),
            text=text,hovertemplate='%{text}<br>Cierre retest: %{y:,.2f}<extra></extra>'))
    return traces


def add_to_figure(figure,overlay,*,start,end):
    if overlay is None:return figure
    traces=marker_traces(overlay['events'],start=start,end=end)
    if isinstance(figure,dict):figure.setdefault('data',[]).extend(traces)
    else:
        for trace in traces:figure.add_trace(trace)
    return figure


def render_overlay(st,project,*,stages,region_ids,cutoff,start,end,prefix):
    """Optional UI; disabled means no source load or canonical replay at all."""
    enabled=st.checkbox('Mostrar REACTIONs BTC · 15m',value=False,key=prefix+'enabled')
    if not enabled:return None
    mode=st.selectbox('Fuente de REACTIONs',SOURCES,index=0,key=prefix+'source',format_func=lambda x:{
        'HLL_HISTORICAL':'Histórico de estos 60 días · igual que HLL',
        'JOURNAL':'REACTIONs guardadas en el journal'}[x])
    sides=st.multiselect('Sentido de las REACTIONs',['LONG','SHORT'],default=['LONG','SHORT'],key=prefix+'sides')
    only_near=st.checkbox('Solo REACTIONs con nivel cerca de estas bandas',value=False,key=prefix+'near')
    tolerance=st.selectbox('Cercanía máxima fuera de la banda · %',(0.,.05,.10,.20),index=2,
        key=prefix+'tolerance',format_func=lambda x:'Solo dentro' if x==0 else f'{x:.2f} %')
    if st.button('Actualizar REACTIONs',key=prefix+'refresh'):clear_caches()
    try:
        spinner=getattr(st,'spinner',None)
        with spinner('Cargando REACTIONs históricas…') if callable(spinner) else nullcontext():
            frame,source=load_reactions(project,mode)
        visible=visible_reactions(frame,start=max(int(start),source['coverage_start']),end=min(int(end),source['coverage_end']),
                                  cutoff=int(cutoff),sides=sides)
        compared=compare_bands(visible,stages,region_ids=region_ids,tolerance_pct=tolerance)
    except (OSError,ValueError,KeyError,ImportError,sqlite3.Error) as exc:
        st.info('No se pudo abrir la capa de REACTIONs: '+str(exc))
        return None
    selected=compared.loc[compared.near_band].copy() if only_near else compared
    st.caption(f"{source['label']} · {len(selected)} REACTIONs visibles; {int(selected.near_band.sum())} cerca de banda. "
        'Marcas en el cierre que confirma el retest; cercanía calculada con el nivel original y la banda conocida al retest.')
    if mode=='JOURNAL':
        st.caption('El journal muestra lo que quedó guardado; una ventana sin registros no certifica cobertura histórica completa.')
    if selected.empty:st.caption('No hay REACTIONs bajo este corte y estos filtros.')
    return dict(events=selected,source=source,cutoff=int(cutoff),tolerance_pct=float(tolerance),prefix=prefix)


def render_table(st,overlay,*,width=None):
    if overlay is None:return
    x=overlay['events'];width={} if width is None else width
    with st.expander('REACTIONs visibles · coincidencia con bandas'):
        if x.empty:st.caption('Sin REACTIONs visibles bajo los filtros actuales.');return
        view=pd.DataFrame({'Conocida UTC':x.known_ts.map(utc),'Retest UTC':x.retest_ts.map(utc),
            'Sentido':x.side,'Tramo':x.phase,'Nivel swing':x.swing_price,'Cierre retest':x.retest_close,
            'Bandas cercanas':x.matched_band_count,'Distancia a banda %':x.gap_to_band_pct,
            'Relación':x.match_status,'Bandas ID':x.matched_region_ids,'Clave original':x.event_id})
        st.dataframe(view,**width)
        st.caption('Coincidencia geométrica y temporal; no demuestra que sea el mismo extremo físico ni una ventaja operativa.')
        exported=x[['event_id','touch_key','symbol','side','status','retest_ts','known_ts','phase','swing_price','retest_close',
            'source','nearest_region_id','band_low_at_retest','band_high_at_retest','stage_known_from',
            'gap_to_band_pct','matched_region_ids','matched_band_count','match_status']]
        st.download_button('Descargar REACTIONs visibles',exported.to_csv(index=False).encode('utf-8-sig'),
            file_name='BTC_REACTIONS_BANDAS_VISIBLES.csv',mime='text/csv',key=overlay['prefix']+'download')
