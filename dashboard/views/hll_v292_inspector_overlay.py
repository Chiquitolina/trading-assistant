"""Pure helpers to show V2.9.2 PRE touches and POST survival in existing inspector.

Never write labels back to causal historic CSV; no access to live market inputs.
"""
from pathlib import Path
import hashlib
import json
import pandas as pd

V292 = Path('reports/horizontal_level_mechanisms_v292/runs/core15_mechanisms_v292')
LABELS = {
 'DIRECTRIZ_ROTA':'Directriz previa rota',
 'RANGO_CRUCES':'Rango con cruces múltiples',
 'MINIMOS_DESCENDENTES':'Mínimos descendentes',
 'ASCENSO_IRREGULAR':'Ascenso irregular',
 'RECUPERACION_V':'Recuperación en V',
 'BASE_CON_PRESION_FINAL':'Base con presión final',
 'PRESION_TESTS_REPETIDOS':'Presión con tests independientes',
 'ASCENSO_SOSTENIDO':'Presión ascendente sostenida',
 'APROXIMACION_IMPULSIVA':'Aproximación impulsiva',
 'MIXTA_OTRA':'Mixta / otra',
 'SIN_COBERTURA':'Cobertura PRE insuficiente',
}


def read_v292(original_path, folder, original):
    path=folder/'mechanism_attempts.csv'
    if not path.is_file():return None,None,None
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    actual=hashlib.sha256(original_path.read_bytes()).hexdigest()
    if manifest.get('source_prehash') != actual:
        raise ValueError('V2.9.2 calculada contra un PRE distinto: no se mezcla')
    df=pd.read_csv(path,low_memory=False)
    if (df.attempt_id.isna().any() or df.attempt_id.duplicated().any() or
        len(df)!=len(original) or set(df.attempt_id)!=set(original.attempt_id)):
        raise ValueError('V2.9.2: IDs faltantes o duplicados')
    extras=[f'{prop}_{w}' for w in (24,48,96) for prop in (
        'mechanism','touch_v26_independent','physical_overlap_clusters','confirmed_pivots',
        'median_pivot_gap_bars','pivot_slope_atr_per_bar','progress_low_atr',
        'close_efficiency','early_drawdown_atr','late_recovery_atr','pullback_depth_ratio')]
    if set(extras)-set(df):raise ValueError('V2.9.2: métricas PRE incompletas')
    t=pd.read_csv(folder/'pre_contacts.csv',low_memory=False)
    s=pd.read_csv(folder/'flip_survival_1m.csv',low_memory=False)
    if not t.empty and (t.duplicated(['attempt_id','window','contact_num']).any() or
                        not set(t.attempt_id).issubset(set(original.attempt_id))):
        raise ValueError('V2.9.2: contactos duplicados o ajenos')
    if not s.empty and (s.duplicated(['attempt_id','horizon_min']).any() or
                        not set(s.attempt_id).issubset(set(original.attempt_id))):
        raise ValueError('V2.9.2: horizontes duplicados o ajenos')
    return df[['attempt_id']+extras],t,s


def overlay_pre_contacts(fig, selected, pre_ts):
    """Adds point markers only at original PRE event clocks, never future resolver."""
    import plotly.graph_objects as go
    if selected is None or selected.empty:return fig
    if (selected.touch_closed_ts.astype('int64')>int(pre_ts)).any():
        raise ValueError('V2.9.2: contacto POST incrustado en PRE')
    fig.add_trace(go.Scatter(
       x=pd.to_datetime(selected.touch_open_ts,unit='ms',utc=True),
       y=selected.touch_price.astype(float),
       mode='markers+text', text=selected.contact_num.astype(str),textposition='top center',
       textfont=dict(size=10,color='#ffda83'),
       marker=dict(symbol='circle-open',size=12,color='#ffd16d',line=dict(width=2)),
       name='Tests independientes PRE (V2.6)',
       customdata=selected[['depth_atr','rejection_atr','resolver_pre']].astype(str).to_numpy(),
       hovertemplate='Test PRE #%{text}<br>Precio: %{y}<br>Profundidad ATR: %{customdata[0]}'+
                     '<br>Rechazo ATR: %{customdata[1]}<br>Estado conocido en PRE: %{customdata[2]}<extra></extra>'))
    return fig


def overlay_physical_clusters(fig, row, bars, window):
    """Visual gray markers for contiguous physical overlaps, NOT independent tests."""
    import plotly.graph_objects as go
    tf=1_800_000
    pre=int(row['pre_closed_ts'])
    prebars=bars[(bars.timestamp>=pre-int(window)*tf)&(bars.timestamp<pre)].copy()
    if len(prebars)!=int(window) or (prebars.timestamp.diff().dropna()!=tf).any():return fig
    hit=((prebars.low<=float(row['zone_high']))&
         (prebars.high>=float(row['zone_low']))).to_numpy(bool)
    starts=[j for j,yes in enumerate(hit) if yes and (j==0 or not hit[j-1])]
    if not starts:return fig
    chosen=prebars.iloc[starts]
    fig.add_trace(go.Scatter(x=pd.to_datetime(chosen.timestamp,unit='ms',utc=True),
       y=chosen.high.astype(float),mode='markers',
       marker=dict(size=7,symbol='circle-open',color='#94a3b8'),
       name='Solapes físicos PRE (no independientes)',
       hovertemplate='Primer solape de grupo PRE<br>Precio: %{y}<br>No es un test V2.6 independiente<extra></extra>'))
    return fig
