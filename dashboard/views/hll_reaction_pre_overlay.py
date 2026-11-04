"""Overlay histórico de REACTIONs 15m en el inspector PRE HLL (solo lectura).

NO reimplementa el detector: ejecuta por AST únicamente las tres funciones
causales originales de dashboard/app.py, junto con SwingDetector original.
Velas 15m derivadas estrictamente de históricos HLL 1m cerrados.
No utiliza Redis, PM2, operaciones LIVE, métricas POST ni etiquetas futuras.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

TF1 = 60_000
TF15 = 15 * TF1
TF30 = 30 * TF1
MINUTE_DIR = Path('reports/horizontal_level_full290_v301/minutes')
MANIFEST_DIR = Path('reports/horizontal_level_full290_v301/minutes_manifest')
DEFAULT_TOLERANCE_PCT = 0.10
DEFAULT_DEPARTURE_PCT = 0.20
DEFAULT_MAX_AGE_MIN = 360
# Configuración original del scanner: 3x3 con prominencia 0. Variantes solo research.
SWING_SHAPES = ((2, 2), (3, 3), (3, 4), (4, 4), (5, 5))
PROMINENCE_GRID_PCT = (0.0, 0.05, 0.10, 0.20)
# 360m retest age + 3+3 swing candles + a safety margin for the first pivot.
WARMUP_MINUTES = 540
ORIGINAL_FUNCTIONS = (
    '_prepare_confirmed_swing_retest_candles',
    'get_volume_exhaustion_swing_confirmation_info',
    '_find_confirmed_swing_retest',
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for buf in iter(lambda: f.read(1024*1024), b''):
            h.update(buf)
    return h.hexdigest()


@lru_cache(maxsize=3)
def canonical_functions(source_path_string: str, source_mtime_ns: int):
    """Extrae las funciones originales sin ejecutar el módulo Streamlit."""
    from engine.live.research.swing_detector import SwingDetector
    source_path = Path(source_path_string)
    source = source_path.read_text(encoding='utf-8-sig')
    tree = ast.parse(source, filename=str(source_path))
    defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if missing := set(ORIGINAL_FUNCTIONS) - defs.keys():
        raise ValueError('Faltan funciones originales de Reaction: '+', '.join(sorted(missing)))
    selected = []
    for name in ORIGINAL_FUNCTIONS:
        node = copy.deepcopy(defs[name])
        node.decorator_list = []
        selected.append(node)
    env = {'pd': pd, 'np': np}
    compiled = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(compiled, filename=str(source_path), mode='exec'), env)
    return env, SwingDetector, file_sha256(source_path)


def load_verified_minute(path: Path, manifest: Path):
    """Valida el histórico HLL: no rellenar ni ocultar huecos."""
    if not path.is_file() or not manifest.is_file():
        raise FileNotFoundError('Histórico 1m HLL o su manifiesto ausente')
    meta = json.loads(manifest.read_text(encoding='utf-8'))
    if meta.get('sha256') != file_sha256(path):
        raise ValueError('Hash del histórico 1m no coincide con manifiesto')
    # El scanner causal no usa volumen para detectar swing ni primer retest.
    raw = pd.read_csv(path, usecols=['timestamp','open','high','low','close'], low_memory=False)
    if raw.empty or len(raw) != int(meta['bars']):
        raise ValueError('Cantidad de velas 1m no coincide con manifiesto')
    for col in raw:
        raw[col] = pd.to_numeric(raw[col], errors='raise')
    if not np.isfinite(raw.to_numpy(dtype=float)).all():
        raise ValueError('Histórico 1m contiene valores inválidos')
    timestamps = raw.timestamp.to_numpy(dtype=np.int64)
    if ((timestamps % TF1) != 0).any() or (np.diff(timestamps) <= 0).any():
        raise ValueError('Timestamps 1m inválidos, fuera de orden o repetidos')
    if (raw[['open','high','low','close']].le(0).any().any()
            or raw.high.lt(raw[['open','close','low']].max(axis=1)).any()
            or raw.low.gt(raw[['open','close','high']].min(axis=1)).any()):
        raise ValueError('OHLC 1m inválido')
    return raw


def closed_prefix(tape: pd.DataFrame, start_ms: int, cutoff_ms: int):
    """Solo velas completas antes de PRE; cobertura exacta o fallo explícito."""
    if cutoff_ms <= start_ms or start_ms % TF1 or cutoff_ms % TF1:
        raise ValueError('Intervalo temporal inválido')
    x = tape.loc[(tape.timestamp >= start_ms) & (tape.timestamp < cutoff_ms)].copy()
    count = (cutoff_ms - start_ms)//TF1
    if (len(x) != count or x.empty or int(x.timestamp.iloc[0]) != start_ms
            or int(x.timestamp.iloc[-1]) != cutoff_ms - TF1
            or not (np.diff(x.timestamp.to_numpy(dtype=np.int64)) == TF1).all()):
        raise ValueError('INCOMPLETE_1M_PRE: falta histórico o hay huecos antes del corte')
    return x.reset_index(drop=True)


def fifteen_from_one_minute(prefix: pd.DataFrame) -> pd.DataFrame:
    """Construye 15m OHLC solo a partir de 15 minutos completos."""
    if prefix.empty or len(prefix) % 15 or int(prefix.timestamp.iloc[0]) % TF15:
        raise ValueError('La ventana 1m debe estar alineada a 15m')
    g = prefix.assign(group=(prefix.timestamp // TF15)*TF15).groupby('group', sort=True)
    out = g.agg(timestamp=('timestamp','min'), open=('open','first'),
                high=('high','max'), low=('low','min'), close=('close','last'),
                count=('timestamp','size')).reset_index(drop=True)
    if (out['count'].ne(15).any() or
            not (np.diff(out.timestamp.to_numpy(dtype=np.int64)) == TF15).all()):
        raise ValueError('INCOMPLETE_15M_PRE: vela 15m sin 15 minutos completos')
    return out.drop(columns=['count'])


def replay_canonical_pre(tape: pd.DataFrame, pre_ms: int, first_visible_ms: int,
                         *, env: dict, swing_detector_class, symbol: str,
                         left_bars: int = 3, right_bars: int = 3,
                         min_prominence_pct: float = 0.0,
                         include_first_retests: bool = False):
    """Replay causal de primeras pruebas. Devuelve solo REACTION originales.

    Retest conocido en retest open + 1m; pivote confirmado en cierre 15m.
    No filtra por resultado futuro ni calcula MFE/MAE.
    """
    if pre_ms % TF30 or first_visible_ms % TF30:
        raise ValueError('PRE/ventana 30m desalineados')
    if (left_bars, right_bars) not in SWING_SHAPES:
        raise ValueError('Ventana de swing no permitida')
    if (not np.isfinite(min_prominence_pct) or min_prominence_pct < 0
            or min_prominence_pct > 5.0):
        raise ValueError('Prominencia inválida')
    start = first_visible_ms - WARMUP_MINUTES*TF1
    prefix = closed_prefix(tape, start, pre_ms)
    candles15 = fifteen_from_one_minute(prefix)
    detector = swing_detector_class(left_bars=left_bars,right_bars=right_bars,
                                    min_prominence_pct=min_prominence_pct)
    points = detector.detect_all(candles15.to_dict('records'))
    confirmation = env['get_volume_exhaustion_swing_confirmation_info']
    first_touch = env['_find_confirmed_swing_retest']
    discovered={}
    raw_first_retests=[]
    for point in points:
        info = confirmation(point=point, timeframe_candles=candles15,swing_timeframe='15m')
        if info is None or int(info['actionable_timestamp']) >= pre_ms:
            continue
        side = 'LONG' if point.side == 'LOW' else 'SHORT' if point.side == 'HIGH' else None
        if side is None:
            continue
        swing = {'timeframe':'15m','detector':f'{left_bars}x{right_bars}','signal':side,
                 'pivot_timestamp':int(point.pivot_timestamp),
                 'actionable_timestamp':int(info['actionable_timestamp']),
                 'pivot_price':float(point.price),
                 'entry_price':float(info['confirmation_close']),
                 'pivot_to_confirmation_pct':float(info['move_pct']),
                 'prominence_pct':point.prominence_pct}
        retest=first_touch(prefix,swing,DEFAULT_TOLERANCE_PCT,
                           DEFAULT_DEPARTURE_PCT,DEFAULT_MAX_AGE_MIN)
        if not retest or (retest['status'] != 'REACTION' and not include_first_retests):
            continue
        known_ts=int(retest['retest_timestamp'])+TF1
        if not (first_visible_ms <= known_ts <= pre_ms):
            continue
        # Coincide con la clave causal de normalize_reactions del scanner online.
        key=(symbol,side,int(retest['retest_timestamp']))
        item={
            'prominence_pct':float(point.prominence_pct),
            'swing_shape':f'{left_bars}x{right_bars}',
            'event_key':'|'.join(map(str,key)),'symbol':symbol,'side':side,
            'status':str(retest['status']),'pivot_ts':int(point.pivot_timestamp),
            'swing_price':float(point.price),
            'actionable_ts':int(info['actionable_timestamp']),
            'departure_ts':int(retest['departure_timestamp']),
            'retest_ts':int(retest['retest_timestamp']),
            'known_ts':known_ts,'retest_close':float(retest['retest_close']),
            'retest_price':float(retest['retest_price']),
            'max_departure_pct':float(retest['max_departure_pct']),
            'penetration_pct':float(retest['penetration_pct']),
            'source':('HISTORICAL_CANONICAL_REPLAY_15M_3X3'
                      if (left_bars,right_bars,min_prominence_pct)==(3,3,0.0)
                      else 'HISTORICAL_RESEARCH_VARIANT_15M'),
        }
        if include_first_retests:
            raw_first_retests.append(item)
        else:
            discovered.setdefault(key,item)
    if include_first_retests:
        return sorted(raw_first_retests,key=lambda r:(r['known_ts'],r['event_key'],r['pivot_ts']))
    return sorted(discovered.values(),key=lambda r:(r['known_ts'],r['event_key']))


def inspect_pre(root: Path, symbol: str, pre_ms: int, pre_candles: int,
                *, left_bars: int = 3, right_bars: int = 3,
                min_prominence_pct: float = 0.0):
    """API sin Streamlit; puede cubrirse con st.cache_data desde el visor."""
    root=Path(root)
    if not symbol or '/' in symbol or '\\' in symbol or '..' in symbol:
        raise ValueError('Símbolo inválido')
    if pre_candles not in (48,96,144,192,288,384):
        raise ValueError('Ventana PRE no permitida')
    tape=load_verified_minute(root/MINUTE_DIR/(symbol+'_1m.csv'),
                              root/MANIFEST_DIR/(symbol+'.json'))
    source=root/'dashboard/app.py'
    env, swing_class, source_hash=canonical_functions(str(source),source.stat().st_mtime_ns)
    events=replay_canonical_pre(tape,pre_ms,pre_ms-pre_candles*TF30,
                                env=env,swing_detector_class=swing_class,symbol=symbol,
                                left_bars=left_bars,right_bars=right_bars,
                                min_prominence_pct=min_prominence_pct)
    return {'events':events,'source_hash':source_hash,
            'count':len(events),'cutoff_ms':pre_ms,'first_visible_ms':pre_ms-pre_candles*TF30,
            'swing_shape':f'{left_bars}x{right_bars}',
            'min_prominence_pct':min_prominence_pct}


def add_markers(fig, events, sides=('LONG','SHORT'), *, variant_label='3x3'):
    """Solo agrega Scatter. Nunca altera el gráfico 30m ni sus marcas POST."""
    import plotly.graph_objects as go
    sides=set(sides)
    for side,color,symbol in [('LONG','#18c68c','triangle-up'),('SHORT','#f26c72','triangle-down')]:
        rows=[r for r in events if r['side']==side and side in sides]
        if not rows:
            continue
        fig.add_trace(go.Scatter(
            x=[pd.Timestamp(int(r['known_ts']),unit='ms',tz='UTC') for r in rows],
            y=[float(r['retest_close']) for r in rows],mode='markers',
            marker={'color':color,'size':10,'symbol':symbol,'line':{'width':1,'color':'#111827'}},
            name='REACTION '+side+' · 15m'+('' if variant_label=='3x3' else ' · '+variant_label),
            customdata=[[r['event_key'],pd.Timestamp(r['pivot_ts'],unit='ms',tz='UTC').isoformat(),
                         float(r['swing_price']),pd.Timestamp(r['known_ts'],unit='ms',tz='UTC').isoformat()]
                        for r in rows],
            hovertemplate='<b>REACTION '+side+'</b><br>Conocida: %{customdata[3]}<br>'
                          'Swing original: %{customdata[1]}<br>Precio swing: %{customdata[2]:.8f}'
                          '<br>Cierre retest: %{y:.8f}<br>Clave: %{customdata[0]}<extra></extra>'))
    return fig
