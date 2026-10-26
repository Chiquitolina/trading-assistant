"""Streamlit read-only explorer for the frozen horizontal cross research V2.2."""
from pathlib import Path
import json
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from horizontal_breakout_lab import Archive, TF
from horizontal_level_lifecycle_v21 import load_30m
from horizontal_level_cross_research_v22 import DEST


def _time(ts):
    return pd.Timestamp(int(ts),unit='ms',tz='UTC')


def render_horizontal_cross_v22(root, atlas_run=None):
    root=Path(root)
    st.markdown('---')
    st.subheader('🔬 Fase B · Ataques, cruces y volumen')
    st.caption('Censo independiente: todos los CROSS_UP / CROSS_DOWN del atlas, más aproximaciones y ataques. '
               'No filtra ganadores. Factores conocidos al cierre 30m; desenlaces solo desde entrada posterior con 1m completo.')
    folder=root/DEST
    folders=sorted([p for p in folder.iterdir() if (p/'freeze.json').is_file()],
                   key=lambda p:p.stat().st_mtime,reverse=True) if folder.is_dir() else []
    if not folders:
        st.info('Todavía no existe un escaneo V2.2. Ejecutá `horizontal_level_cross_research_v22.py --plan` '
                'y `--scan` usando una corrida V2.1 ya congelada.')
        return
    chosen=st.selectbox('Corrida de cruces y ataques',folders,format_func=lambda p:p.name,key='hllcross-run')
    frozen=json.loads((chosen/'freeze.json').read_text())
    signals=pd.read_csv(chosen/'signals.csv')
    if signals.empty:
        st.warning('El escaneo no encontró eventos con esa cobertura.');return
    st.caption(f"Atlas utilizado: {Path(frozen['atlas_run']).name} · "
               f"Señales: {len(signals):,} · Símbolos: {signals.symbol.nunique()} · "
               f"Niveles: {signals.level_id.nunique()}")
    st.markdown('**Universo, sin optimización por resultado**')
    cnt=signals.groupby(['stage','origin'],dropna=False).agg(
        events=('event_id','size'),symbols=('symbol','nunique'),
        levels=('level_id','nunique'),
        median_volume_ratio=('vol_ratio','median'),
        median_crossups_pre=('prior_cross_up','median')).reset_index()
    st.dataframe(cnt.round(3),use_container_width=True,hide_index=True,height=280)
    evaluations=sorted([p for p in (chosen/'evaluations').iterdir() if (p/'audit.json').exists()],
                       key=lambda p:p.stat().st_mtime,reverse=True) if (chosen/'evaluations').is_dir() else []
    if evaluations:
        evaldir=st.selectbox('Evaluación posterior 1m',evaluations,format_func=lambda p:p.name,key='hllcross-eval')
        aud=json.loads((evaldir/'audit.json').read_text())
        st.caption(f"Cobertura 1m: {aud['n_complete_1m']} / {aud['outcomes']} combinaciones evento/horizonte/TP. "
                   'Una combinación censurada NO es SL.')
        comp=pd.read_csv(evaldir/'comparison.csv')
        cols=st.columns(3)
        with cols[0]: factor=st.selectbox('Factor a comparar',
                    ['ALL','zone_quality_flag','vol_bin','distance_pre_atr_bin','range_atr_bin','level_age_bin',
                     'prior_cross_up_bin','prior_tests_bin','prior_retests_directional_bin','prior_failed_cross_up_bin'],
                    key='hllcross-factor')
        with cols[1]:hor=st.selectbox('Horizonte',sorted(comp.horizon_min.dropna().unique().tolist()),
                                      key='hllcross-h')
        with cols[2]:tp=st.selectbox('TP (%)',sorted(comp.tp_pct.dropna().unique().tolist()),
                                     key='hllcross-tp')
        view=comp[(comp.factor==factor)&(comp.horizon_min==hor)&(comp.tp_pct==tp)]
        if not view.empty:
            keep=['stage','factor_value','n_events','n_symbols','n_symbol_days',
                  'n_complete_1m','n_tp','n_sl','n_ambiguous','n_time','n_censored',
                  'tp_pct_full_denominator','tp_pct_resolved','median_volume_ratio','median_mfe_pct',
                  'median_mae_pct','median_net_pct']
            st.dataframe(view[[c for c in keep if c in view]],
                         use_container_width=True,hide_index=True,height=360)
            if (view.n_complete_1m<30).any():
                st.warning('Alguno de los grupos tiene menos de 30 desenlaces completos; interpretar solo descriptivamente.')
        st.caption('El porcentaje sobre todos los completos incluye TIME_EXIT y SAME_1M ambiguo en el denominador. '
                   'El porcentaje entre resueltos excluye TIME_EXIT y SAME_1M ambiguo. '
                   'No hay slippage, funding ni simulación de posiciones simultáneas.')
    else:
        st.info('Todavía no hay recorridos 1m evaluados para esta corrida. '
                'Los eventos y sus factores PRE ya están disponibles para inspección.')
    st.markdown('**Inspector causal del evento**')
    c1,c2=st.columns(2)
    with c1:
        stage=st.selectbox('Etapa del nivel',sorted(signals.stage.unique()),key='hllcross-stage')
    group=signals[signals.stage==stage]
    with c2:
        sym=st.selectbox('Símbolo',sorted(group.symbol.unique()),key='hllcross-symbol')
    group=group[group.symbol==sym].sort_values('known_closed_ts',ascending=False)
    ids=group.event_id.tolist()
    lookup=group.set_index('event_id')
    eid=st.selectbox('Evento',ids,format_func=lambda k:
        f"{_time(lookup.loc[k,'known_closed_ts']).strftime('%d/%m %H:%M UTC')} · "
        f"zona {lookup.loc[k,'center']:.7g} · "
        f"vol x{lookup.loc[k,'vol_ratio']:.2f}" if pd.notna(lookup.loc[k,'vol_ratio']) else
        f"{_time(lookup.loc[k,'known_closed_ts']).strftime('%d/%m %H:%M UTC')} · volumen NO DISPONIBLE",
        key='hllcross-event')
    e=lookup.loc[eid]
    keys=['stage','origin','zone_low','zone_high','previous_close','close','atr_pre',
          'vol_ratio','vol_pre5_20_ratio','vol_rank_prev20_pct','prior_cross_up',
          'prior_failed_cross_up','prior_tests','prior_retests_directional',
          'distance_pre_atr','bar_close_position','feature_state']
    st.dataframe(pd.DataFrame({'Métrica': keys, 'Valor': [e[k] for k in keys]}),use_container_width=True)
    blind=st.radio('Corte temporal',('HASTA_EVENTO (ciego)','MOSTRAR_POST (solo visual)'),horizontal=True,
                   key='hllcross-blind')
    pre=st.select_slider('Velas anteriores',options=[40,80,120,160,240],value=120,key='hllcross-pre')
    try:
        atlas_manifest=json.loads((Path(frozen['atlas_run'])/'run_config.json').read_text())
        candle_path=Path(atlas_manifest['candles_dir']) if atlas_manifest.get('candles_dir') else None
        with Archive(root,candle_path) as arc:
            bars,src=load_30m(arc,sym)
        stamp=int(e.known_closed_ts)
        end=stamp if blind.startswith('HASTA_EVENTO') else stamp+48*TF
        sub=bars[(bars.timestamp+TF<=end)&(bars.timestamp+TF>stamp-pre*TF)].copy()
        if sub.empty:st.warning('No hay velas suficientes para este evento.')
        else:
            t=pd.to_datetime(sub.timestamp,unit='ms',utc=True)
            fig=make_subplots(rows=2,cols=1,shared_xaxes=True,vertical_spacing=.035,
                              row_heights=[.80,.20])
            fig.add_trace(go.Candlestick(x=t,open=sub.open,high=sub.high,low=sub.low,
                         close=sub.close,name='OHLC 30m',
                         increasing_line_color='#10b981',decreasing_line_color='#fb7185',
                         increasing_fillcolor='#10b981',decreasing_fillcolor='#fb7185'),row=1,col=1)
            fig.add_trace(go.Bar(x=t,y=sub.volume,name='Volumen',marker_color='#64748b'),row=2,col=1)
            from pandas import Timestamp
            formed=int(e.formation_closed_ts)
            if formed<=end:
                start=max(int(sub.timestamp.iloc[0]),formed)
                fig.add_trace(go.Scatter(x=[_time(start),_time(end)],
                     y=[float(e.zone_low)]*2,mode='lines',line=dict(color='#fbbf24',width=2),
                     name='Límite de zona inferior'),row=1,col=1)
                fig.add_trace(go.Scatter(x=[_time(start),_time(end)],
                     y=[float(e.zone_high)]*2,mode='lines',line=dict(color='#fbbf24',width=2,dash='dash'),
                     name='Límite de zona superior'),row=1,col=1)
            fig.add_vline(x=_time(stamp),line_color='#38bdf8',line_dash='dash',row=1,col=1)
            fig.add_trace(go.Scatter(x=[_time(stamp)],y=[float(e.close)],name='Evento conocido',
                        mode='markers',marker=dict(size=12,color='#38bdf8',symbol='diamond')),
                        row=1,col=1)
            fig.update_layout(height=560,margin=dict(l=12,r=12,t=22,b=10),
                              xaxis_rangeslider_visible=False)
            st.plotly_chart(fig,use_container_width=True)
            st.caption('El marcador azul solo existe cuando cerró la vela 30m. '
                       'El gráfico POST no forma parte de las características de la señal. '
                       f'OHLCV: {src}.')
    except Exception as exc:
        st.error(f'No se pudo generar el gráfico de la investigación: {exc}')
    st.download_button('Exportar universo de señales V2.2',signals.to_csv(index=False).encode(),
                       file_name=f'{chosen.name}_signals.csv',mime='text/csv',key='hllcross-export')
