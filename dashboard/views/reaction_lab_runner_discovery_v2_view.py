"""Compact, read-only research workspace for continuous REACTION Runner Discovery V2.

No live collector modification, external API call, trade, auto refresh or PM2 process.
Heavy source SQLite work runs ONLY when user explicitly clicks Generate/Refresh.
"""
from __future__ import annotations

import html
from pathlib import Path
import sqlite3

from dashboard.services.runner_discovery_dna import (
    runner_labels, compare_traits, assign_families, family_report, FAMILY_ORDER,
)

from reaction_lab_runner_discovery_v2 import (
    FEATURE_LABELS, HORIZONS, TARGETS, build_dataset,
    outcome_at_horizon, project_results_dir, readonly_uri,
)

COLORS = {
    "TP_FIRST": "#20b99a",
    "SL_FIRST": "#ef7177",
    "NO_HIT": "#e2a857",
    "AMBIGUOUS": "#8391a6",
    "INSUFFICIENT": "#4e5f76",
}
LABELS = {
    "TP_FIRST": "TP primero",
    "SL_FIRST": "SL primero",
    "NO_HIT": "No tocó",
    "AMBIGUOUS": "Misma vela",
    "INSUFFICIENT": "Sin cobertura",
}
BUCKETS = {
    "max_departure_pct": [-float("inf"), .5, 1, 2, 3, 5, float("inf")],
    "retest_distance_pct": [-float("inf"), 0.025, .05, .1, .2, .5, float("inf")],
    "relative_volume": [-float("inf"), .5, 1, 2, 4, float("inf")],
    "nearest_opposing_room_pct": [-float("inf"), .5, 1, 2, 3, 5, float("inf")],
    "opposing_room_1h_pct": [-float("inf"), .5, 1, 2, 3, 5, float("inf")],
    "side_adjusted_strength_vs_btc_4h": [-float("inf"), -2, -.5, 0, .5, 2, float("inf")],
    "market_breadth_1h": [-float("inf"), 20, 40, 60, 80, float("inf")],
    "breadth_delta_1h": [-float("inf"), -20, -5, 0, 5, 20, float("inf")],
    "market_breadth_4h": [-float("inf"), 20, 40, 60, 80, float("inf")],
    "rsi14_15m": [-float("inf"), 20, 30, 40, 50, 60, 70, 80, float("inf")],
    "rel_volume_15m": [-float("inf"), .5, 1, 2, 4, float("inf")],
}


STYLE = """
<style>
.rdv2-hero {border:1px solid rgba(117,156,184,.20);border-radius:18px;
 padding:19px 22px 16px;background:linear-gradient(117deg,rgba(21,57,72,.75),rgba(28,39,64,.52));margin:0 0 13px;}
.rdv2-tag {font-size:11px;color:#77e6cb;font-weight:750;letter-spacing:1.7px;}
.rdv2-title {font-size:27px;font-weight:770;color:#f4f8fc;margin:5px 0 3px;letter-spacing:-.5px;}
.rdv2-sub {font-size:13px;color:#b7c7d6;line-height:1.45;}
.rdv2-metric {border:1px solid rgba(140,164,190,.20);border-radius:14px;padding:16px 15px;
  background:linear-gradient(135deg,#203545,#202b3c);min-height:104px;}
.rdv2-metric span {display:block;font-size:12px;color:#a9b8cc;}
.rdv2-metric strong {display:block;color:#f0f6fa;font-size:27px;font-weight:760;letter-spacing:-.6px;margin-top:3px;}
.rdv2-metric small {color:#90a1b4;font-size:11px;}
.rdv2-label {font-size:13px;color:#a7bacc;}
@media(max-width:800px){.rdv2-title{font-size:22px}.rdv2-metric strong{font-size:21px}}
</style>
"""


def _load_csv(path, stamp):
    import pandas as pd
    df = pd.read_csv(path, low_memory=False)
    for field in ("runner_catalog", "path_count", "minute_group", "entry_observed_ts",
                  "entry_structural_ts", "observed_delay_min", "observed_bars", "retest_ts"):
        if field in df:
            df[field] = pd.to_numeric(df[field], errors="coerce")
    return df


def _classify(frame, *, mode, tp, horizon):
    import pandas as pd
    prefix = "obs" if mode == "observed" else "struct"
    result_col, min_col = f"{prefix}_tp{tp}_result", f"{prefix}_tp{tp}_min"
    values = []
    for result, minute, bars in zip(
            frame[result_col], frame[min_col],
            frame["observed_bars"] if mode == "observed" else [360] * len(frame)):
        available = bars if mode == "observed" else 360
        values.append(outcome_at_horizon(result, minute, horizon, available=available))
    return pd.Series(values, index=frame.index, dtype="string")


def _metric(title, value, note):
    return ("<div class='rdv2-metric'><span>" + html.escape(str(title)) +
            "</span><strong>" + html.escape(str(value)) + "</strong><small>" +
            html.escape(str(note)) + "</small></div>")


def _pct(num, denom):
    return f"{100*num/denom:.1f}%" if denom else "—"


def _chart_style(fig, *, height=295):
    fig.update_layout(
        height=height, margin=dict(l=5,r=12,t=12,b=8),
        paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#adbecd",size=12),
        legend=dict(orientation="h",y=-.16),
        xaxis=dict(showgrid=False,zeroline=False),
        yaxis=dict(gridcolor="rgba(143,166,185,.15)",zeroline=False),
    )
    return fig


def _pretty_bucket(interval):
    left,right = interval.left,interval.right
    def fmt(n):
        if n == float("-inf"): return "−∞"
        if n == float("inf"): return "+∞"
        return f"{n:g}"
    return f"{fmt(left)} a {fmt(right)}"


def _detail_bars(project, event_key):
    from reaction_lab_auto_paths import decode_path
    db=Path(project)/"reports/reaction_lab/auto_paths_v1.sqlite3"
    with sqlite3.connect(readonly_uri(db),uri=True) as con:
        row=con.execute("SELECT path_blob FROM events WHERE event_key=?",(event_key,)).fetchone()
    return decode_path(row[0]) if row and row[0] else []


def _signal_chart(project, event, *, mode, tp, horizon, show_sl=True):
    import plotly.graph_objects as go
    import pandas as pd
    import streamlit as st
    bars=_detail_bars(project,event["event_key"])
    if not bars:
        st.info("No hay velas archivadas para este evento.")
        return
    # Structural barriers are computed from retest_close, not the next bar OPEN.
    # Observed-proxy barriers are computed from the first observable bar OPEN.
    base = "entry_observed_open" if mode == "observed" else "retest_close"
    timestamp = "entry_observed_ts" if mode == "observed" else "entry_structural_ts"
    reference = event.get(base)
    entry = event.get(timestamp)
    if pd.isna(reference) or pd.isna(entry):
        st.info("Este evento no tiene una entrada evaluable bajo el modo elegido.")
        return
    reference=float(reference)
    entry=int(entry)
    selected=[b for b in bars if b[0]>=entry][:int(horizon)]
    if not selected:
        st.info("Sin velas posteriores a la observación.")
        return
    xs=pd.to_datetime([x[0] for x in selected],unit="ms",utc=True)
    fig=go.Figure(go.Candlestick(
        x=xs,open=[b[1] for b in selected], high=[b[2] for b in selected],
        low=[b[3] for b in selected],close=[b[4] for b in selected],
        increasing_line_color="#21bd9e",decreasing_line_color="#eb6f7a",
        increasing_fillcolor="#21bd9e",decreasing_fillcolor="#eb6f7a",
        name="1m OHLC"))
    side=event["side"]
    tpprice=reference*(1 + tp/100 if side=="LONG" else 1 - tp/100)
    slprice=reference*(.99 if side=="LONG" else 1.01)
    lines = [(reference,"#b5c7d7","Referencia" if not show_sl else "Entrada"),
             (tpprice,"#20b99a",f"+{tp}% favorable" if not show_sl else f"TP {tp}%")]
    if show_sl:
        lines.append((slprice,"#ef7177","SL 1%"))
    for price, col, name in lines:
        fig.add_hline(y=price,line_color=col,line_dash="dash",line_width=1.25,
                      annotation_text=name,annotation_position="top right")
    fig.update_layout(xaxis_rangeslider_visible=False, showlegend=False, hovermode="x unified")
    st.plotly_chart(_chart_style(fig,height=355),use_container_width=True,config={"displaylogo":False})
    if show_sl:
        st.caption("OHLC de 1m; si TP y SL están en la misma vela no se puede conocer su orden. "
                   "Las líneas son barreras teóricas, no órdenes ejecutadas.")
    else:
        st.caption("Movimiento favorable contado desde la referencia indicada, incluso si hubo retrocesos. "
                   "No representa una operación ejecutada.")





def _render_triple_runner_filter(project, working, pool, *, mode, tp, horizon):
    """All REACTIONs first, runner outcomes only afterward: avoid winner selection."""
    import pandas as pd
    import streamlit as st

    st.markdown("##### ⛰️ Triple techo / triple piso · geometría 15m PRE")
    st.caption("Busca pico–valle–pico–valle–pico (o su inverso), con DOS valles "
               "confirmados, similares y de profundidad acotada. Excluye techos "
               "separados por grandes desplomes o por una tendencia extensa. "
               "Confirmado: tercer swing 3×3 conocido PRE. Formación: tercer rechazo "
               "observado PRE sin tres velas de confirmación. No exige romper neckline.")
    try:
        from reaction_runner_triple_top_pre_v1 import label_path, generate, verify_labels
    except ImportError as exc:
        st.error(f"Falta el detector de geometría PRE: {exc}")
        return
    path=label_path(project)
    if not path.is_file():
        st.info('Esta versión usa etiquetas NUEVAS de geometría (V2); los 179 anteriores '
                'no eran triples techos compactos verificados. Ejecutá '
                '`python3 reaction_runner_triple_top_pre_v1.py --run` y `--audit`.')
        if st.button('Detectar geometría PRE en TODAS las REACTIONs',key='rdv2_triple_generate_v2'):
            with st.spinner('Clasificando todas las REACTIONs offline (sin usar outcomes)...'):
                try:generate(project)
                except Exception as exc:
                    st.error(f'Clasificación PRE fallida: {exc}')
                    return
            st.rerun()
        return

    @st.cache_data(show_spinner=False,max_entries=2)
    def _read_triple_validated(root,mtime,manifest_mtime):
        return verify_labels(root)
    try:
        manifest_path=path.with_suffix('.json')
        triples=_read_triple_validated(str(project),path.stat().st_mtime_ns,
                                       manifest_path.stat().st_mtime_ns)
        fields=['event_key','pattern_pre','reason','top_bottom_1_ts',
                'top_bottom_2_ts','top_bottom_3_ts','level_price','level_tolerance',
                'third_confirmation_ts','neckline_price','valley_1_ts','valley_2_ts',
                'valley_1_depth_atr','valley_2_depth_atr','valley_price_diff_atr']
        base=working.merge(triples[fields],on='event_key',how='left',validate='one_to_one')
        if base.pattern_pre.isna().any():
            raise ValueError('Hay REACTIONs sin label PRE; regenerá la cohorte con --run')
        # Runner classification is attached only after independent PRE tagging.
        available=pool[['event_key','es_runner','dna_minuto','familia_adn']].copy()
        if available.event_key.duplicated().any():
            raise ValueError('Event IDs duplicados en evaluación runner')
        base=base.merge(available,on='event_key',how='left',validate='one_to_one',
                        suffixes=('','_runner'))
    except Exception as exc:
        st.error(f'Etiquetas triples PRE inválidas: {exc}')
        st.caption('Reejecutá `python3 reaction_runner_triple_top_pre_v1.py --run` '
                   'y luego `--audit`; los labels anteriores V1 no se reutilizan.')
        return
    states_top=('TRIPLE_TOP_CONFIRMED_PRE','TRIPLE_TOP_FORMING_PRE')
    states_bottom=('TRIPLE_BOTTOM_CONFIRMED_PRE','TRIPLE_BOTTOM_FORMING_PRE')
    evaluable=('NO_PATTERN_PRE',)+states_top+states_bottom
    valid=base.pattern_pre.isin(evaluable)
    top=base.pattern_pre.isin(states_top)
    bottom=base.pattern_pre.isin(states_bottom)
    counted=base.es_runner.notna()
    hits=base.es_runner.eq(True).fillna(False)
    misses=base.es_runner.eq(False).fillna(False)
    short=base.side.eq('SHORT');long=base.side.eq('LONG')
    metrics=st.columns(4,gap='small')
    metrics[0].metric('SHORT · triples en todas las REACTIONs',int(top.sum()),
                      help='Incluye runner, no runner y sin outcome evaluable')
    metrics[1].metric('SHORT · triples que fueron runners',int((top&hits).sum()),
                      help=f'{int((top&hits).sum())}/{int((top&counted).sum())} evaluables con patrón')
    metrics[2].metric('SHORT · triples que NO fueron runners',int((top&misses).sum()),
                      help='Solo evaluaciones runner con cobertura; los faltantes no son pérdidas')
    metrics[3].metric('LONG · triples pisos en todas las REACTIONs',int(bottom.sum()))
    st.caption(f'{len(base)} REACTIONs en el universo seleccionado · '
               f'{int(valid.sum())} con estructura PRE evaluable · '
               f'{int((~valid).sum())} con histórico/valor desconocido · '
               f'{int((~counted).sum())} sin desenlace runner evaluable. '
               'Cada valor cuenta REACTIONs, no formaciones independientes.')
    options={
        'Triple techo SHORT · confirmado o formación':states_top,
        'Triple techo SHORT · confirmado':(states_top[0],),
        'Triple techo SHORT · en formación':(states_top[1],),
        'Triple piso LONG · confirmado o formación':states_bottom,
        'Triple piso LONG · confirmado':(states_bottom[0],),
        'Triple piso LONG · en formación':(states_bottom[1],),
        'Todos los triples (ambas direcciones)':states_top+states_bottom,
        'Sin patrón geométrico PRE':('NO_PATTERN_PRE',),
        'Sin histórico/valor PRE suficiente':('UNKNOWN_HISTORY','UNKNOWN_PRICE'),
        'Todas las REACTIONs':None,
    }
    choice=st.selectbox('Filtrar TODAS las REACTIONs por geometría 15m',list(options),
                        key='rdv2_triple_pattern_filter_v2')
    filtered=base if options[choice] is None else base.loc[base.pattern_pre.isin(options[choice])]
    outcome_choice=st.selectbox('Desenlace del runner en el horizonte seleccionado',
        ['Todos (incluye sin cobertura)','Llegó al objetivo · RUNNER',
         'No llegó al objetivo · NO RUNNER','Sin evaluación'],
        key='rdv2_triple_outcome_filter_v2')
    if outcome_choice.startswith('Llegó'):
        filtered=filtered.loc[filtered.es_runner.eq(True)]
    elif outcome_choice.startswith('No llegó'):
        filtered=filtered.loc[filtered.es_runner.eq(False)]
    elif outcome_choice=='Sin evaluación':
        filtered=filtered.loc[filtered.es_runner.isna()]
    filtered=filtered.sort_values('retest_ts',ascending=False).copy()
    st.markdown(f'**{len(filtered)} REACTIONs** coinciden con los filtros. '
                f'Entre ellas, **{int(filtered.es_runner.eq(True).sum())} runners**, '
                f'**{int(filtered.es_runner.eq(False).sum())} no-runners** y '
                f'**{int(filtered.es_runner.isna().sum())} sin evaluación** '
                f'(objetivo +{tp}%, ventana {horizon}m).')
    if filtered.empty:
        st.info('Sin eventos para esta combinación de filtros.')
        return
    overview=filtered.head(250).copy()
    overview['UTC']=pd.to_datetime(overview.retest_ts,unit='ms',utc=True).dt.strftime('%d/%m %H:%M')
    overview['Desenlace']=overview.es_runner.map({True:'RUNNER',False:'NO RUNNER'}).fillna('SIN COBERTURA')
    st.dataframe(overview[['UTC','symbol','side','pattern_pre','Desenlace','event_key']].rename(
        columns={'symbol':'Símbolo','side':'Dirección','pattern_pre':'Geometría',
                 'event_key':'Event ID'}),use_container_width=True,hide_index=True,height=290)
    keys=filtered.event_key.astype(str).head(250).tolist()
    selected=st.selectbox('Inspeccionar REACTION y desenlace',keys,
                          key='rdv2_triple_inspect_all_v2')
    event=filtered.loc[filtered.event_key.astype(str).eq(selected)].iloc[0]
    is_runner=event.get('es_runner')
    status='SIN COBERTURA' if pd.isna(is_runner) else ('RUNNER' if bool(is_runner) else 'NO RUNNER')
    st.markdown(f"**{event['symbol']} · {event['side']} · {status}**  "
                f"· +{tp}% en {horizon}m · {event['pattern_pre']}")
    st.caption('Arriba: patrón PRE con picos 1/2/3 y valles A/B. '
               'Debajo: trayectoria 1m posterior y barreras TP/SL de referencia. '
               'MFE posterior y desenlace NO intervienen en la detección del patrón.')
    _triple_15m_chart(project,event)
    if not pd.isna(is_runner):
        state=_classify(pd.DataFrame([event]),mode=mode,tp=tp,horizon=horizon).iloc[0]
        label={'TP_FIRST':'TP antes de SL','SL_FIRST':'SL antes de TP',
               'NO_HIT':'Ni TP ni SL','AMBIGUOUS':'TP y SL en la misma vela',
               'INSUFFICIENT':'Cobertura insuficiente'}.get(str(state),str(state))
        st.markdown(f'**Desenlace teórico TP +{tp}% / SL −1%:** {label}. '
                    'Runner es MFE favorable, independiente del orden TP/SL.')
    else:
        st.warning('Sin cobertura runner: no contamos este evento como fallido.')
    _signal_chart(project,event,mode=mode,tp=tp,horizon=horizon,show_sl=True)
    with st.expander('Exportar todas las REACTIONs de esta selección',expanded=False):
        st.download_button('CSV · geometría PRE + desenlace de cada REACTION',
            filtered.to_csv(index=False).encode('utf-8-sig'),
            file_name='reactions_triple_geometria_pre_v2.csv',mime='text/csv',
            key='rdv2_triple_export_all_v2')
    st.caption('Exploración retrospectiva, no validación prospectiva. '
               'El 15m PRE y el gráfico POST mantienen sus ventanas temporales separadas.')


def _read_triple_pre_candles(project, symbol, event_ts, max_bars=170):
    """Load archived 15m history directly, with no optional gallery dependency.

    The same Full Context / HTF cache and aggregation are used by the causal
    triple-pivot labeler. Every returned candle must have CLOSED before the
    REACTION timestamp, so the inspector cannot accidentally draw future bars.
    """
    from contextlib import closing
    import pandas as pd
    import reaction_structural_level_atlas as atlas
    from reaction_compression_breakout_retest_v4 import _merge_tf

    project = Path(project)
    base = project / atlas.lineage.ROOT_REL
    ctx_path = base / atlas.lineage.CTX_NAME
    hist_path = base / atlas.lineage.HISTORY_NAME
    if not ctx_path.is_file():
        raise FileNotFoundError(f"No existe el SQLite Full Context: {ctx_path}")
    history = atlas.lineage.ro(hist_path) if hist_path.is_file() else None
    try:
        with closing(atlas.lineage.ro(ctx_path)) as ctx:
            df15, _, _ = _merge_tf(ctx, history, str(symbol))
    finally:
        if history is not None:
            history.close()

    if df15 is None or df15.empty:
        return pd.DataFrame(columns=['timestamp','open','high','low','close'])
    bars = df15.reset_index()
    needed = {'timestamp','open','high','low','close'}
    if not needed.issubset(bars.columns):
        raise ValueError(f"Histórico 15m sin columnas: {sorted(needed-set(bars.columns))}")
    ts = pd.to_numeric(bars['timestamp'], errors='coerce')
    # Strict inequality is identical to classify_pre() in the frozen detector.
    bars = bars.loc[ts.notna() & ((ts + 900_000) < int(event_ts))].copy()
    bars = bars.sort_values('timestamp', kind='stable').tail(int(max_bars)).reset_index(drop=True)
    if not bars.empty:
        if (pd.to_numeric(bars.timestamp, errors='raise')+900_000 >= int(event_ts)).any():
            raise ValueError('Inspector PRE intentó incluir una vela futura')
        if bars.timestamp.duplicated().any():
            raise ValueError('Inspector PRE recibió timestamps 15m duplicados')
    return bars


def _triple_15m_chart(project, event):
    """Display the three PRE contacts and common price band above the POST chart."""
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    if not st.checkbox('Mostrar 170 velas 15m anteriores a la REACTION',
                       value=True,key='rdv2_triple_show15m'):
        return
    try:
        # The cache key changes when either Full Context SQLite changes.
        import reaction_structural_level_atlas as atlas
        base=project/atlas.lineage.ROOT_REL
        ctx=base/atlas.lineage.CTX_NAME
        history=base/atlas.lineage.HISTORY_NAME
        times=(ctx.stat().st_mtime_ns if ctx.is_file() else 0,
               history.stat().st_mtime_ns if history.is_file() else 0)

        @st.cache_data(show_spinner=False,max_entries=12)
        def _cached_pre(root,symbol,ts,mtimes):
            return _read_triple_pre_candles(root,symbol,ts,max_bars=170)

        bars=_cached_pre(str(project),str(event['symbol']),int(event['retest_ts']),times)
    except Exception as exc:
        st.warning(f'Histórico PRE 15m no disponible para este gráfico: {exc}')
        return
    if bars.empty:
        st.info('No hay velas 15m PRE para visualizar este evento.')
        return
    t=pd.to_datetime(bars.timestamp,unit='ms',utc=True)
    f=go.Figure(go.Candlestick(x=t,open=bars.open,high=bars.high,
        low=bars.low,close=bars.close,
        increasing_line_color='#20b99a',decreasing_line_color='#ec777c',
        name='15m PRE'))
    state=str(event.get('pattern_pre',''))
    if state.startswith('TRIPLE_'):
        level=float(event['level_price']);tol=float(event['level_tolerance'])
        f.add_hrect(y0=level-tol,y1=level+tol,fillcolor='#e6ad56',opacity=.14,line_width=0)
        f.add_hline(y=level,line_color='#f3b950',line_dash='dash',line_width=1.4,
                    annotation_text='Nivel triple',annotation_position='top left')
        peak_positions=[]
        for i,name in enumerate(('Primero','Segundo','Tercero'),1):
            val=event.get(f'top_bottom_{i}_ts')
            if pd.isna(val) or str(val).strip() in ('','nan'):continue
            peak_close=int(float(val));peak_open=peak_close-900_000
            target='high' if event['side']=='SHORT' else 'low'
            price=bars.loc[pd.to_numeric(bars.timestamp,errors='coerce').eq(peak_open),target]
            if price.empty:
                st.warning(f'Toque PRE #{i} no presente en el histórico local; revisá la cobertura.')
                continue
            x=pd.to_datetime(peak_open,unit='ms',utc=True)
            peak_positions.append(peak_open)
            f.add_trace(go.Scatter(x=[x],y=[float(price.iloc[0])],
                 mode='markers+text',text=[str(i)],textposition='top center',
                 marker=dict(size=13,color='#ffca68',symbol='diamond',
                             line=dict(color='#17202b',width=1)),
                 name=f'{name} toque',
                 hovertemplate=f'Toque {i}: %{{x}}<br>Precio %{{y:.8f}}<extra></extra>'))
        # The two *confirmed* opposite-side swing valleys complete M/W geometry.
        valley_points=[]
        inverse='low' if event['side']=='SHORT' else 'high'
        for vid,letter in enumerate(('A','B'),1):
            vts=event.get(f'valley_{vid}_ts')
            if pd.isna(vts) or str(vts).strip() in ('','nan'):
                continue
            vopen=int(float(vts))-900_000
            val=bars.loc[pd.to_numeric(bars.timestamp,errors='coerce').eq(vopen),inverse]
            if val.empty:
                st.warning(f'Valle {letter} no disponible en la ventana de gráfico')
                continue
            y=float(val.iloc[0])
            valley_points.append((vopen,y))
            f.add_trace(go.Scatter(x=[pd.to_datetime(vopen,unit='ms',utc=True)],y=[y],
                mode='markers+text',text=[letter],textposition='bottom center',
                marker=dict(size=12,color='#65bfff',symbol='circle'),
                name=f'Valle {letter}',hovertemplate=f'Valle {letter}: %{{x}}<br>%{{y:.8f}}<extra></extra>'))
        if len(peak_positions)==3 and len(valley_points)==2:
            highs=[]
            for vts in peak_positions:
                hi=bars.loc[pd.to_numeric(bars.timestamp,errors='coerce').eq(vts),target]
                if not hi.empty:highs.append(float(hi.iloc[0]))
            if len(highs)==3:
                zig=[(peak_positions[0],highs[0]),valley_points[0],
                     (peak_positions[1],highs[1]),valley_points[1],
                     (peak_positions[2],highs[2])]
                f.add_trace(go.Scatter(x=pd.to_datetime([x for x,y in zig],unit='ms',utc=True),
                    y=[y for x,y in zig],mode='lines',
                    line=dict(color='#65bfff',width=2,dash='dot'),
                    name='Geometría M/W'))
            neckline=event.get('neckline_price')
            if pd.notna(neckline) and str(neckline).strip() not in ('','nan'):
                f.add_hline(y=float(neckline),line_dash='dot',line_color='#65bfff',
                            annotation_text='Neckline PRE',annotation_position='bottom right')
        if peak_positions:
            zoom=st.checkbox('Acercar al patrón (los tres toques)',value=True,
                             key='rdv2_triple_zoom')
            if zoom:
                xmin=max(int(bars.timestamp.min()), min(peak_positions)-8*900_000)
                xmax=int(event['retest_ts'])
                f.update_xaxes(range=[pd.to_datetime(xmin,unit='ms',utc=True),
                                     pd.to_datetime(xmax,unit='ms',utc=True)])
        st.caption(f"**Patrón PRE:** {state} · Nivel {level:.8g} "
                   f"(tolerancia ±{tol:.6g}) · "
                   f"{len(peak_positions)}/3 toques visibles. "
                   "CONFIRMED = tercer swing 3×3 confirmado; "
                   "FORMING = tercer rechazo aún sin confirmación 3×3. "
                   "No exige ruptura de neckline.")
    else:
        st.caption(f'Clasificación PRE: {state}; este evento no tiene triple techo/piso etiquetado.')
    f.update_layout(height=470,xaxis_rangeslider_visible=False,
        title=f"{event['symbol']} · {state or 'sin etiqueta'} · 15m PRE",
        margin=dict(l=6,r=8,t=45,b=8),legend=dict(orientation='h',y=-.13))
    st.plotly_chart(_chart_style(f,height=470),use_container_width=True,
                    config={'displaylogo':False,'scrollZoom':True})
    st.caption('Gráfico superior: únicamente velas 15m cerradas PRE. '
               'Los diamantes 1/2/3 son picos; A/B son valles confirmados y la línea azul muestra la M/W. '
               'Gráfico inferior: recorrido 1m POST de la REACTION; no muestra el triple.')

def _render_runner_dna(project, working, *, mode, tp, horizon):
    """Simple descriptive DNA: hit target regardless of any SL or fill."""
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    st.markdown(f"### 🧬 ¿Qué tienen en común los runners de +{tp}%?")
    st.caption(
        f"Movimiento favorable alcanzado en hasta {horizon} minutos, **sin importar si antes hubo retroceso**. "
        + ("La referencia es el cierre del retest (no ejecutable)." if mode == "structural" else
           "Referencia: primer OPEN 1m observado en LIVE (entrada idealizada).")
    )
    try:
        pool = runner_labels(working, mode=mode, target=tp, horizon=horizon)
    except KeyError:
        st.warning("Este CSV es de la versión anterior. Presioná **↻ Actualizar análisis** arriba "
                   "para calcular los nuevos tiempos de runner sin SL. Los collectors LIVE siguen intactos.")
        return
    if pool.empty:
        st.info("No hay eventos con recorrido completo para esta configuración.")
        return
    pool = assign_families(pool)
    runner = pool.loc[pool["es_runner"]].copy()
    n = len(pool); nr = len(runner)
    moments = runner["minute_group"].nunique()
    typical = f"{int(runner['dna_minuto'].median())} min" if nr else "—"
    cells = st.columns(4, gap="small")
    cards = [
        ("Runners encontrados",f"{nr:,}".replace(",","."), f"De {n:,} señales con recorrido evaluable"),
        ("Frecuencia", _pct(nr,n), "Sobre todas las REACTIONs evaluables"),
        ("Tiempo típico al movimiento", typical, "Mediana entre runners"),
        ("Minutos del mercado", f"{moments:,}".replace(",","."), "No todos los eventos son independientes"),
    ]
    for cell,(name,value,note) in zip(cells,cards):
        cell.markdown(_metric(name,value,note),unsafe_allow_html=True)
    if not nr:
        st.info("Ninguna señal alcanzó este objetivo; todavía podés explorar "
                "los triples PRE de TODAS las REACTIONs.")
        _render_triple_runner_filter(project,working,pool,mode=mode,tp=tp,horizon=horizon)
        return

    traits = compare_traits(pool)
    families = family_report(pool)
    left,right=st.columns([1.25,1],gap="large")
    with left:
        st.markdown("#### Rasgos más repetidos")
        st.caption("De cada 100 runners, ¿cuántos tenían este rasgo? Comparado con otras REACTIONs "
                   "que disponen del mismo dato.")
        if traits.empty:
            st.info("Faltan factores con suficiente cobertura para esta comparación.")
        else:
            top = traits.head(6).iloc[::-1]
            fig=go.Figure()
            fig.add_bar(name="Runners",y=top["Rasgo"],x=top["runner_pct"],orientation="h",
                        marker_color="#38bfad",
                        customdata=top[["runner_con_rasgo","runner_con_dato"]].to_numpy(),
                        hovertemplate="%{y}: %{x:.1f}% (%{customdata[0]}/%{customdata[1]})<extra>Runners</extra>")
            fig.add_bar(name="Otras REACTIONs",y=top["Rasgo"],x=top["resto_pct"],orientation="h",
                        marker_color="#7795b0",
                        customdata=top[["resto_con_rasgo","resto_con_dato"]].to_numpy(),
                        hovertemplate="%{y}: %{x:.1f}% (%{customdata[0]}/%{customdata[1]})<extra>Resto</extra>")
            fig.update_layout(barmode="group",xaxis_title="Eventos con el rasgo (%)",showlegend=True,
                              xaxis_range=[0,105],margin=dict(l=160,r=15,t=10,b=35))
            st.plotly_chart(_chart_style(fig,height=330).update_layout(
                margin=dict(l=150,r=15,t=10,b=35)),use_container_width=True,
                config={"displaylogo":False})
            best=traits.iloc[0]
            st.caption(f"Mayor diferencia descriptiva: **{best['Rasgo']}** "
                       f"({best['runner_pct']:.1f}% en runners vs {best['resto_pct']:.1f}% "
                       "en el resto con dato disponible). No demuestra causalidad.")
            with st.expander("Ver todos los rasgos y sus muestras",expanded=False):
                detail=traits[["Rasgo","runner_con_rasgo","runner_con_dato","resto_con_rasgo",
                                "resto_con_dato","runner_pct","resto_pct"]].rename(columns={
                    "runner_con_rasgo":"Runners con rasgo","runner_con_dato":"Runners con dato",
                    "resto_con_rasgo":"Resto con rasgo","resto_con_dato":"Resto con dato",
                    "runner_pct":"Runners %","resto_pct":"Resto %"})
                st.dataframe(detail,hide_index=True,use_container_width=True,height=280)
    with right:
        st.markdown("#### Familias de runners")
        st.caption("Clasificación sencilla según tres rasgos fijos: "
                   "Departure ≥2%, Room 1h ≥2% y fuerza BTC extrema ±2%. "
                   "Cada evento pertenece a una sola familia.")
        fam= families.loc[families["runners"].gt(0)].copy()
        if fam.empty:
            st.info("Sin runners clasificados.")
        else:
            chart=go.Figure(go.Bar(x=fam["runners"],y=fam["familia_adn"],orientation="h",
                                   marker_color="#4ba9cd",text=fam["runners"],textposition="auto"))
            chart.update_layout(xaxis_title="Runners",showlegend=False,
                                margin=dict(l=135,r=12,t=10,b=35),
                                yaxis=dict(autorange="reversed",showgrid=False))
            st.plotly_chart(_chart_style(chart,height=295).update_layout(
                margin=dict(l=125,r=12,t=10,b=35)),use_container_width=True,
                config={"displaylogo":False})
            st.caption("**Contexto incompleto** es una categoría separada: "
                       "no significa que el evento carezca de los rasgos.")
            with st.expander("Frecuencia sobre todas las señales por familia",expanded=False):
                out=families.rename(columns={"familia_adn":"Familia","senales":"REACTIONs",
                                               "runners":"Runners","tasa_runner_pct":"Runner %"})
                st.dataframe(out,hide_index=True,use_container_width=True,height=280)

    st.markdown("#### Explorar runners concretos")
    _render_triple_runner_filter(project,working,pool,mode=mode,tp=tp,horizon=horizon)
    a,b=st.columns([1.5,1],gap="small")
    family_options=["Todas las familias"]+[f for f in FAMILY_ORDER if (runner["familia_adn"]==f).any()]
    family=a.selectbox("Familia",family_options,key="rdv2_dna_family")
    ordering=b.selectbox("Orden",["Más recientes","Más rápidos"],key="rdv2_dna_order")
    events=runner if family=="Todas las familias" else runner.loc[runner["familia_adn"].eq(family)]
    if ordering=="Más rápidos":
        events=events.sort_values(["dna_minuto","retest_ts"],ascending=[True,False])
    else:
        events=events.sort_values("retest_ts",ascending=False)
    st.caption(f"{len(events)} runners en esta selección · mostrando {min(len(events),60)}. "
               "Cada fila se puede localizar por Event ID.")
    display=events.head(60).copy()
    display["UTC"]=pd.to_datetime(display["retest_ts"],unit="ms",utc=True).dt.strftime("%d/%m %H:%M")
    display["Departure %"]=pd.to_numeric(display.get("max_departure_pct"),errors="coerce")
    display["Room 1h %"]=pd.to_numeric(display.get("opposing_room_1h_pct"),errors="coerce")
    st.dataframe(display[["UTC","symbol","side","dna_minuto","familia_adn","Departure %","Room 1h %"]]
                 .rename(columns={"symbol":"Símbolo","side":"Lado","dna_minuto":"Min hasta run",
                                  "familia_adn":"Familia"}),hide_index=True,use_container_width=True,
                 height=265,column_config={"Departure %":st.column_config.NumberColumn(format="%.2f"),
                                          "Room 1h %":st.column_config.NumberColumn(format="%.2f")})
    with st.expander("Inspeccionar gráfico de un runner",expanded=False):
        choices=events["event_key"].astype(str).head(250).tolist()
        if choices:
            key=st.selectbox("Evento",choices,key="rdv2_dna_event")
            row=events.loc[events["event_key"].eq(key)].iloc[0]
            st.caption(f"{row['symbol']} · {row['side']} · +{tp}% en {int(row['dna_minuto'])} min · {row['familia_adn']}")
            _triple_15m_chart(project,row)
            _signal_chart(project,row,mode=mode,tp=tp,horizon=horizon,show_sl=False)
    with st.expander("Exportar este estudio",expanded=False):
        st.download_button("CSV · todas las señales y su familia",pool.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"adn_runner_{tp}pct_{horizon}m.csv",mime="text/csv",key="rdv2_dna_export")
    st.caption("Estudio exploratorio: rasgos definidos de antemano para la vista; se necesitan nuevas fechas "
               "para evaluar poder predictivo. Movimientos de monedas simultáneas comparten mercado.")

def render_runner_discovery_v2(project_root):
    """Drop-in entrypoint for Reaction & Swing Lab in dashboard/app.py."""
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st

    project=Path(project_root).resolve()
    results=project_results_dir(project)
    file=results/"runner_discovery_v2_events.csv"
    manifest_file=results/"manifest.json"
    st.markdown(STYLE,unsafe_allow_html=True)
    st.markdown("""<div class='rdv2-hero'><div class='rdv2-tag'>REACTION LAB  /  INVESTIGACIÓN</div>
       <div class='rdv2-title'>🏃 Runner Discovery <span style='color:#71dfc5'>V2</span></div>
       <div class='rdv2-sub'>Todos los eventos · movimientos, tiempo y riesgo · sin filtros de ganadores ni ejecución LIVE</div>
       </div>""",unsafe_allow_html=True)

    status_col,action_col=st.columns([4,1.3],vertical_alignment="center")
    if manifest_file.exists():
        try:
            import json
            manifest=json.loads(manifest_file.read_text(encoding="utf-8"))
            date=manifest.get("created_utc","")[:16].replace("T"," ")
            status_col.caption(f"Datos generados: {date} UTC · Solo lectura de bases originales")
        except (OSError,ValueError):
            status_col.caption("Resultados guardados · Solo lectura")
    else:
        status_col.caption("Todavía no generaste el primer análisis.")
    refresh=action_col.button("↻ Actualizar análisis",key="rdv2_refresh",use_container_width=True,
                              help="Reconstruye el CSV desde las bases SQLite actuales. No reinicia servicios.")
    if refresh:
        try:
            with st.spinner("Leyendo trayectorias y calculando barreras..."):
                build_dataset(project)
            st.success("Dataset actualizado. Los servicios LIVE no se modificaron.")
        except Exception as exc:
            st.error(f"No se pudo generar Runner Discovery V2: {exc}")
            return
    if not file.exists():
        st.info("Presioná **Actualizar análisis** para construir tu primer dataset con todas las REACTIONs.")
        st.caption("El análisis es manual y no ejecuta órdenes ni agrega procesos PM2.")
        return

    @st.cache_data(show_spinner=False, max_entries=2)
    def cached_read(filename, mtime_ns):
        return _load_csv(filename, mtime_ns)

    try:
        frame=cached_read(str(file),file.stat().st_mtime_ns)
    except (OSError,ValueError,pd.errors.ParserError) as exc:
        st.error(f"Error leyendo el CSV: {exc}")
        return
    if frame.empty:
        st.warning("El archivo no contiene eventos todavía.")
        return

    controls=st.columns([1.65,1,1,1],gap="small")
    mode_label=controls[0].selectbox("Punto de partida",
        ["Referencia retest", "Observación LIVE"],key="rdv2_mode",
        help="Referencia: retest_close, NO ejecutable. LIVE: OPEN 1m posterior a la primera observación.")
    mode="observed" if mode_label == "Observación LIVE" else "structural"
    target=controls[1].selectbox("Objetivo",["+2%", "+3%", "+5%"],key="rdv2_target")
    tp=int(target[1:-1])
    horizon=controls[2].selectbox("Ventana",["15 min", "30 min", "60 min", "120 min", "180 min", "360 min"],
                                   index=4,key="rdv2_horizon")
    minutes=int(horizon.split()[0])
    side=controls[3].selectbox("Dirección",["Todas","LONG","SHORT"],key="rdv2_side")
    working=frame.copy()
    with st.expander("Filtros adicionales · fecha, origen y símbolo",expanded=False):
        x1,x2,x3=st.columns([1,1.15,1.4])
        origin=x1.selectbox("Procedencia",["Todas","LIVE_OBSERVED","RECOVERED_WINDOW"],key="rdv2_origin")
        days=sorted(working["date_utc"].dropna().astype(str).unique().tolist())
        date_value=x2.select_slider("Desde",options=days,value=days[0],key="rdv2_start") if days else None
        symbol_search=x3.text_input("Símbolo",placeholder="Ej. BTC, ARC...",key="rdv2_symbol").strip().upper()
        st.caption("Los filtros afectan resultados y tablas; no se descartan silenciosamente eventos sin recorrido.")
        if origin!="Todas": working=working.loc[working["observation_type"].eq(origin)]
        if date_value: working=working.loc[working["date_utc"].ge(date_value)]
        if symbol_search: working=working.loc[working["symbol"].astype(str).str.upper().str.contains(symbol_search,regex=False)]
    if side!="Todas": working=working.loc[working["side"].eq(side)]
    selected=working.loc[working["quality"].eq("COMPLETE_VALID")].copy()
    if mode=="observed":
        selected=selected.loc[selected["observed_quality"].eq("OBSERVED_PROXY")]
    if selected.empty:
        st.warning("No hay trayectorias completas y evaluables para estos filtros.")
        return
    selected["Resultado"]=_classify(selected,mode=mode,tp=tp,horizon=minutes)
    insufficient=int(selected["Resultado"].eq("INSUFFICIENT").sum())
    eligible=selected.loc[~selected["Resultado"].eq("INSUFFICIENT")].copy()
    counts=eligible["Resultado"].value_counts().to_dict()
    n=len(eligible); wins=int(counts.get("TP_FIRST",0)); losses=int(counts.get("SL_FIRST",0))
    prefix="obs" if mode=="observed" else "struct"
    times=pd.to_numeric(eligible.loc[eligible["Resultado"].eq("TP_FIRST"),f"{prefix}_tp{tp}_min"],errors="coerce")
    median_min=f"{int(times.median())} min" if len(times) else "—"
    tab_dna,tab_episodes,tab_pan,tab_signals,tab_factors=st.tabs([
        "🧬 ADN del Runner", "🧩 Episodios", "📊 Panorama", "🔎 Señales", "📈 Factores"])
    with tab_episodes:
        from dashboard.views.reaction_lab_movement_episodes_view import render_movement_episodes
        render_movement_episodes(project)
    with tab_dna:
        _render_runner_dna(project, working, mode=mode, tp=tp, horizon=minutes)
    with tab_pan:
        col=st.columns(4,gap="small")
        cards=[("Señales evaluables",f"{n:,}".replace(",","."),f"{len(working):,} REACTIONs filtradas"),
               ("TP antes de SL",_pct(wins,n),f"{wins} eventos · +{tp}%"),
               ("SL antes de TP",_pct(losses,n),f"{losses} eventos · −1%"),
               ("Tiempo típico al TP",median_min,"Mediana de TP-first")]
        for cell,(name,value,detail) in zip(col,cards):
            cell.markdown(_metric(name,value,detail),unsafe_allow_html=True)
        caption="Referencia al cierre del retest: movimiento estructural, no entrada operable." if mode=="structural" else (
            "Proxy LIVE: entrada idealizada en primer OPEN 1m no anterior a known_ts y first_seen_ms; sin latencia, fills ni comisiones.")
        st.caption(f"{caption} · {len(working)-len(selected)} sin evaluación inicial · {insufficient} sin cobertura para {minutes} min · {len(eligible)} denominador válido.")
    with tab_pan:
        chart_l, chart_r=st.columns([1,1.25],gap="large")
        with chart_l:
            st.markdown("**¿Qué pasó con las señales?**")
            labels=[v for v in ("TP_FIRST","SL_FIRST","NO_HIT","AMBIGUOUS") if counts.get(v,0)]
            fig=go.Figure(go.Pie(labels=[LABELS[k] for k in labels],values=[counts[k] for k in labels],
                                 marker=dict(colors=[COLORS[k] for k in labels]),hole=.68,
                                 textinfo="percent",textfont=dict(size=12),sort=False))
            fig.update_layout(showlegend=True,legend=dict(orientation="h",y=-.08),
                              annotations=[dict(text=f"{n:,}".replace(",","."),x=.5,y=.5,font_size=21,showarrow=False)])
            st.plotly_chart(_chart_style(fig),use_container_width=True,config={"displaylogo":False})
        with chart_r:
            st.markdown("**¿En cuánto tiempo llegan al TP?**")
            cut=[15,30,60,120,180,360]
            limits=[x for x in cut if x<=minutes]
            values=[]; prev=0
            for lim in limits:
                num=int((times<=lim).sum())
                values.append(num-prev);prev=num
            fig=go.Figure(go.Bar(x=[f"{(0 if j==0 else limits[j-1])}–{x}m" for j,x in enumerate(limits)],
                                 y=values,marker_color="#39b8a7",text=values,textposition="outside",
                                 hovertemplate="%{x}: %{y} TP-first<extra></extra>"))
            fig.update_layout(yaxis_title="Eventos",showlegend=False)
            st.plotly_chart(_chart_style(fig),use_container_width=True,config={"displaylogo":False})
        l,r=st.columns([1.3,1])
        with l:
            st.markdown("**LONG vs SHORT**")
            by_side=[]
            for direction, grp in eligible.groupby("side"):
                hit=int(grp["Resultado"].eq("TP_FIRST").sum())
                by_side.append({"Lado":direction,"Eventos":len(grp),"TP primero":hit,
                                "TP-first %":round(100*hit/len(grp),1)})
            st.dataframe(pd.DataFrame(by_side),hide_index=True,use_container_width=True,height=130)
        with r:
            st.markdown("**Calidad de la evidencia**")
            st.caption(f"{len(frame):,} eventos en el CSV completo · "+
                       f"{int(frame['quality'].eq('COMPLETE_VALID').sum()):,} trayectorias de 360m verificadas. "
                       "Las señales simultáneas NO son independientes.")
            if n:
                groups=eligible.loc[eligible["Resultado"].eq("TP_FIRST"),"minute_group"].nunique()
                st.caption(f"Los {wins} TP-first corresponden a {groups} minutos distintos del mercado.")
        st.info("**Cómo leerlo:** TP-first significa que el extremo OHLC favorable tocó el objetivo antes del SL de 1%. "
                "Si ambos se tocaron en la misma vela, queda ambiguo. No hay órdenes simuladas ni equity.",icon="ℹ️")

    with tab_signals:
        row1,row2,row3=st.columns([1,1.45,1.3])
        class_choice=row1.selectbox("Resultado",["Todos","TP primero","SL primero","No tocó","Misma vela"],key="rdv2_class")
        symbol_filter=row2.text_input("Buscar símbolo o Event ID",placeholder="Ej. ARCUSDT",key="rdv2_search").strip().upper()
        sort_choice=row3.selectbox("Ordenar",["Más recientes","TP más rápido","Mayor recorrido"],key="rdv2_sort")
        table=eligible.copy()
        if class_choice!="Todos":
            code={v:k for k,v in LABELS.items()}[class_choice]
            table=table.loc[table["Resultado"].eq(code)]
        if symbol_filter:
            table=table.loc[table["event_key"].astype(str).str.upper().str.contains(symbol_filter,regex=False)]
        if sort_choice=="Más recientes": table=table.sort_values("retest_ts",ascending=False)
        elif sort_choice=="TP más rápido": table=table.sort_values(f"{prefix}_tp{tp}_min",ascending=True,na_position="last")
        else: table=table.sort_values("mfe_360_pct",ascending=False)
        st.caption(f"Mostrando {min(len(table),250)} de {len(table)} señales. Filtrá para encontrar un evento concreto.")
        display=table.head(250).copy()
        display["UTC"]=pd.to_datetime(display["retest_ts"],unit="ms",utc=True,errors="coerce").dt.strftime("%d/%m %H:%M")
        display["Tiempo TP / SL"] =pd.to_numeric(display[f"{prefix}_tp{tp}_min"],errors="coerce")
        display["Resultado"]=display["Resultado"].map(LABELS)
        st.dataframe(display[["UTC","symbol","side","Resultado","Tiempo TP / SL","mfe_360_pct","event_key"]]
                     .rename(columns={"symbol":"Símbolo","side":"Lado","mfe_360_pct":"Run max 360m (%)","event_key":"Event ID"}),
                     hide_index=True,use_container_width=True,height=300,
                     column_config={"Run max 360m (%)":st.column_config.NumberColumn(format="%.2f%%")})
        with st.expander("Ver un evento en el gráfico 1m",expanded=False):
            if table.empty: st.caption("Sin eventos con estos filtros.")
            else:
                keys=table["event_key"].astype(str).head(250).tolist()
                selected_key=st.selectbox("Evento",keys,key="rdv2_event_detail")
                event=table.loc[table["event_key"].eq(selected_key)].iloc[0]
                st.markdown(f"**{html.escape(str(event['symbol']))} · {event['side']}**   —   {LABELS.get(event['Resultado'],event['Resultado'])}")
                _signal_chart(project,event,mode=mode,tp=tp,horizon=minutes)
        with st.expander("Exportar resultados de la selección",expanded=False):
            csv_out=eligible.copy()
            st.download_button("Descargar CSV de eventos filtrados",csv_out.to_csv(index=False).encode("utf-8-sig"),
                file_name="runner_discovery_v2_filtrado.csv",mime="text/csv",key="rdv2_download")

    with tab_factors:
        st.markdown("**¿Qué cambia entre las señales que llegan al TP y las que no?**")
        st.caption("Comparación descriptiva: buckets fijos, sin buscar combinaciones ni crear reglas automáticamente. "
                   "Los factores faltantes permanecen vacíos; no se reconstruyen con información futura.")
        factors=[k for k in FEATURE_LABELS if k in eligible.columns and
                 pd.to_numeric(eligible[k],errors="coerce").notna().sum() >= 20]
        if not factors:
            st.info("Todavía no hay suficiente contexto causal archivado para comparar factores.")
        else:
            feature=st.selectbox("Elegí una variable para comparar",factors,
                 format_func=lambda x:FEATURE_LABELS[x],key="rdv2_factor")
            feature_series=pd.to_numeric(eligible[feature],errors="coerce")
            data=eligible.loc[feature_series.notna()].copy()
            data["bucket"]=pd.cut(feature_series.loc[data.index],bins=BUCKETS[feature],right=False)
            data["win"] =data["Resultado"].eq("TP_FIRST").astype(int)
            grouped=data.groupby("bucket",observed=True).agg(eventos=("win","size"),tp=("win","sum"))
            grouped=grouped.loc[grouped.eventos >= 10].copy()
            total_with_feature=int(feature_series.notna().sum())
            st.caption(f"Dato disponible en {total_with_feature}/{n} ({_pct(total_with_feature,n)}) eventos evaluables. "
                       "Se muestran grupos de al menos 10 eventos.")
            if grouped.empty:
                st.info("No hay grupos con 10 eventos o más para esta variable.")
            else:
                grouped["rate"]=100*grouped.tp/grouped.eventos
                grouped["bucket_label"]=[_pretty_bucket(x) for x in grouped.index]
                fig=go.Figure(go.Bar(x=grouped["bucket_label"],y=grouped["rate"],marker_color="#45b7a8",
                    customdata=grouped["eventos"],
                    hovertemplate="Grupo %{x}<br>TP primero: %{y:.1f}%<br>Eventos: %{customdata}<extra></extra>"))
                base=100*data["win"].mean() if len(data) else 0
                fig.add_hline(y=base,line_dash="dot",line_color="#e2a857",annotation_text=f"Base con dato {base:.1f}%")
                fig.update_layout(yaxis_title="TP-first (%)",showlegend=False)
                st.plotly_chart(_chart_style(fig,height=290),use_container_width=True,
                                config={"displaylogo":False})
                report=grouped[["bucket_label","eventos","tp","rate"]].rename(columns={
                    "bucket_label":"Rango","eventos":"Eventos","tp":"TP primero","rate":"Tasa TP %"})
                st.dataframe(report,hide_index=True,use_container_width=True,height=min(280,38*(len(report)+1)))
            st.warning("Una diferencia en el gráfico no demuestra una ventaja predictiva. "
                "Todavía falta validación posterior en fechas nuevas, dependencia entre eventos y costos.",icon="⚠️")

    with st.expander("Metodología y cobertura del dataset",expanded=False):
        counts=frame["quality"].value_counts().reset_index()
        counts.columns=["Calidad de trayectoria","Eventos"]
        st.dataframe(counts,hide_index=True,use_container_width=True,height=200)
        st.caption("INCLUDED: todas las REACTIONs del scanner. COMPLETE_VALID requiere 360 velas contiguas de 1m "
            "con OHLC válidos; las demás no cuentan como pérdidas. Horizonte desde el comienzo de la base elegida.")
        st.caption("RUN: máximo favorable sobre retest_close. OBSERVED: idealización de la primera apertura posterior a "
            "known_ts y first_seen_ms; RECOVERED_WINDOW no entra en el modo LIVE. Empates intrabar quedan ambiguos.")
        st.caption("Solo investigación. Sin exchange, órdenes, comisiones, funding, slots ni validación fuera de muestra.")
