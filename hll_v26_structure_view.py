#!/usr/bin/env python3
"""Read-only Streamlit inspector for V2.6 resistance structure episodes.

Launch from repository root: streamlit run hll_v26_structure_view.py
All file paths can be changed in sidebar. No writes/orders/network.
"""
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title='Horizontal Structure Lab V2.6',layout='wide')
st.title('Horizontal Structure Lab · V2.6')
st.caption('Investigación causal de resistencias. Características conocidas y resultados futuros en paneles separados. Nada de esto genera órdenes.')

out_dir=Path(st.sidebar.text_input('Carpeta de resultados V2.6',
    'reports/horizontal_level_structure_lab_v26/runs/core15_extended_v26'))
cand_dir=Path(st.sidebar.text_input('Carpeta OHLCV 30m',
    'reports/horizontal_level_lifecycle_v21/candles_aug20_oct25_core15_btc'))
if not (out_dir/'breakout_attempts.csv').exists():
    st.warning('No se encontraron resultados V2.6. Ejecutá primero --run en la consola.')
    st.stop()

@st.cache_data(show_spinner=False)
def read_csv(path):return pd.read_csv(path,low_memory=False)

at=read_csv(out_dir/'breakout_attempts.csv')
res=read_csv(out_dir/'attempt_outcomes.csv')
tx=read_csv(out_dir/'structure_transitions.csv')
touches=read_csv(out_dir/'touches.csv')
joined=at.merge(res[['attempt_id','status','accepted_closed_ts','retest_closed_ts',
                     'defense_closed_ts','failed_closed_ts','flip_sustained_closed_ts']],on='attempt_id',validate='one_to_one')
syms=sorted(joined.symbol.unique().tolist())
sym=st.sidebar.selectbox('Símbolo',syms)
stat_opts=sorted(joined.status.unique())
status=st.sidebar.multiselect('Estados retrospectivos',stat_opts,default=stat_opts)
pressure=st.sidebar.selectbox('Presión anterior', ['Todos','Sí','No'])
min_touches=st.sidebar.number_input('Toques independientes mínimos previos',min_value=0,max_value=50,value=0)
view=joined[(joined.symbol==sym)&joined.status.isin(status)&
            (joined.prior_independent_touches>=min_touches)].copy()
if pressure!='Todos':view=view[view.pressure_build_up==(1 if pressure=='Sí' else 0)]
view=view.sort_values(['known_closed_ts','level_id'])
st.caption(f'{len(view)} intentos encontrados con los filtros. Pueden compartir vela y trayectoria.')
if view.empty:st.stop()
view['choice']=(view.known_utc.astype(str)+' | '+view.status.astype(str)+' | '+
                view.attempt_id.astype(str).str[:10])
selection=st.selectbox('Intento / fecha UTC',view.choice.tolist(),index=0)
record=view[view.choice==selection].iloc[0]
aid=record.attempt_id

file=cand_dir/f'{sym}_30m.csv'
if not file.exists():
    st.error(f'No están las velas 30m: {file}')
    st.stop()

@st.cache_data(show_spinner=False)
def get_candles(path):
    data=pd.read_csv(path)
    data['utc']=pd.to_datetime(data.timestamp,unit='ms',utc=True)
    return data

bars=get_candles(file)
btime=int(record.known_closed_ts)
position=bars.index[bars.timestamp==btime-1800000]
if position.empty:
    st.error('No hay vela 30m correspondiente al evento')
    st.stop()
i=int(position[0]); start=max(0,i-60); stop=min(len(bars),i+50)
window=bars.iloc[start:stop]
fig=go.Figure(go.Candlestick(x=window.utc,open=window.open,high=window.high,low=window.low,close=window.close,
    increasing_line_color='#17A673',decreasing_line_color='#E05A62',name='30m'))
fig.add_hrect(y0=float(record.zone_low),y1=float(record.zone_high),fillcolor='#f2b03e',
              opacity=.19,line_width=0)
fig.add_hline(y=float(record.zone_low),line_color='#d9952e',line_dash='dash',line_width=1)
fig.add_hline(y=float(record.zone_high),line_color='#d9952e',line_dash='dash',line_width=1)
colors={'CROSS_UP':'#2E73D2','ACCEPTED_ABOVE':'#2BA27E',
        'RETEST_CONTACT_FROM_ABOVE':'#D49D22','FLIP_CONFIRMED':'#2BA27E',
        'FLIP_SUSTAINED_4B':'#149e6d','FAILED_BREAKOUT':'#D95757',
        'RETEST_FAILED':'#D95757','FLIP_LATER_LOST':'#D95757',
        'DIRECT_NO_RETEST_24B':'#7289AD'}
selected_tx=tx[tx.attempt_id==aid].sort_values('known_closed_ts')
for _,t in selected_tx.iterrows():
    pos=pd.to_datetime(int(t.known_closed_ts),unit='ms',utc=True)
    if window.utc.min()<=pos<=window.utc.max():
        fig.add_shape(type='line',x0=pos,x1=pos,y0=0,y1=1,xref='x',yref='paper',
                      line=dict(color=colors.get(t.transition,'#8B8FA7'),width=1.3,dash='dot'))
        fig.add_annotation(x=pos,y=1,yref='paper',text=t.transition,showarrow=False,
                           textangle=-65,font=dict(size=9),yanchor='bottom')
fig.update_layout(height=520,xaxis_rangeslider_visible=False,
                  title=f'{sym} · {record.known_utc} · {record.status}',
                  xaxis_title='UTC',yaxis_title='Precio',legend=dict(orientation='h'))
st.plotly_chart(fig,use_container_width=True)

col1,col2=st.columns(2)
with col1:
    st.subheader('Conocido al cierre del CROSS_UP')
    hidden={'choice','status','accepted_closed_ts','retest_closed_ts','defense_closed_ts',
            'failed_closed_ts','flip_sustained_closed_ts'}
    vals={k:v for k,v in record.to_dict().items() if k not in hidden}
    st.dataframe(pd.DataFrame([{'variable':k,'valor':str(v)} for k,v in vals.items()]),
                 hide_index=True,use_container_width=True,height=440)
with col2:
    st.subheader('Transiciones confirmadas posteriormente')
    st.warning('Estas transiciones NO estaban disponibles al abrir la operación original.')
    st.dataframe(selected_tx[['transition','known_utc','note']],hide_index=True,
                 use_container_width=True,height=250)
    level_touches=touches[(touches.level_id==record.level_id)&
        (touches.touch_closed_ts<record.known_closed_ts)].sort_values('touch_closed_ts')
    st.caption(f'{len(level_touches)} tests independientes previos para este nivel.')
    st.dataframe(level_touches[['touch_utc','side','depth_atr','rejection_atr','resolver_type','resolver_known_ts']],
                 hide_index=True,use_container_width=True,height=230)

st.divider()
st.subheader('Resumen exploratorio completo')
agg=read_csv(out_dir/'structure_by_prior_features.csv')
dim=st.selectbox('Agrupar por',sorted(agg.dimension.unique()))
st.dataframe(agg[agg.dimension==dim],hide_index=True,use_container_width=True)
st.caption('Frecuencias descriptivas de episodios dependientes. Sin cartera, sin retorno ni significancia causal demostrada.')
