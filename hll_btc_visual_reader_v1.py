"""Read-only, hash-verified BTC 1m visuals for frozen HLL plots.

No network, no backfill, no changes to structural scans. Missing coverage fails closed.
"""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
import pandas as pd

RELATIVE_RUN = Path('reports/reaction_lab/btc_visual_candles_v1/btc_oct24_nov08_visual_v1')
MINUTE = 60_000
EXPECTED_COLUMNS = ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'quoteVolume')


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_verified(folder: Path) -> tuple[pd.DataFrame, dict]:
    folder = Path(folder)
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('version') != 'hll-btc-visual-candles-v1.0'
            or manifest.get('symbol') != 'BTCUSDT'
            or manifest.get('integrity') != 'CONTIGUOUS_1M_VERIFIED'
            or manifest.get('mode') != 'RUN_OFFLINE'):
        raise ValueError('BTC visual manifest inválido')
    csv_path = folder/'BTCUSDT_1m.csv'
    if _sha(csv_path) != manifest.get('files_sha256', {}).get('BTCUSDT_1m.csv'):
        raise ValueError('BTC visual CSV alterado o hash incorrecto')
    bars = pd.read_csv(csv_path, float_precision='round_trip')
    if tuple(bars.columns) != EXPECTED_COLUMNS:
        raise ValueError('Columnas de velas BTC 1m inesperadas')
    start, end = int(manifest['start_ms']), int(manifest['end_exclusive_ms'])
    count = (end-start)//MINUTE
    if (end <= start or (end-start)%MINUTE
            or len(bars) != count or len(bars) != int(manifest['bars'])
            or len(bars) != int(manifest['expected_bars'])):
        raise ValueError('Cantidad de velas BTC incongruente')
    ts = bars.timestamp.to_numpy(dtype='int64')
    if (int(ts[0]) != start or int(ts[-1]) != end-MINUTE
            or not (ts[1:]-ts[:-1] == MINUTE).all()):
        raise ValueError('Hueco, duplicado o borde incorrecto en BTC 1m')
    for field in ('open','high','low','close','volume','quoteVolume'):
        bars[field] = pd.to_numeric(bars[field],errors='raise')
    if (bars[['open','high','low','close']].isna().any().any()
            or not (bars.high >= bars[['open','low','close']].max(axis=1)).all()
            or not (bars.low <= bars[['open','high','close']].min(axis=1)).all()
            or (bars.low <= 0).any() or (bars.volume < 0).any()):
        raise ValueError('OHLC 1m inválido')
    return bars, manifest


def plot_window(bars: pd.DataFrame, manifest: dict, cutoff: int, days: int,
                timeframe: str, max_bars: int = 3200) -> pd.DataFrame:
    minutes = {'1m':1,'3m':3,'5m':5,'15m':15}
    if timeframe not in minutes:
        raise ValueError('Temporalidad 1m inválida')
    size = minutes[timeframe]
    cutoff, start = int(cutoff), int(cutoff)-int(days)*86_400_000
    if start < int(manifest['start_ms']) or cutoff > int(manifest['end_exclusive_ms']):
        raise ValueError('Este corte y período quedan fuera del histórico BTC 1m verificado (24/10–08/11 01:00 UTC)')
    # Do not use a candle that has not yet CLOSED at the HLL historical cut.
    b = bars.loc[(bars.timestamp >= start) & (bars.timestamp+MINUTE <= cutoff)].copy()
    expected = int(days)*1440
    if len(b) != expected or b.empty:
        raise ValueError('Ventana BTC 1m incompleta; no interpolar ni rellenar huecos')
    if size == 1:
        out = b[['timestamp','open','high','low','close']]
    else:
        bucket=(b.timestamp.astype('int64')//(size*MINUTE))*(size*MINUTE)
        g=b.assign(bucket=bucket).groupby('bucket',sort=True)
        a=g.agg(timestamp=('timestamp','min'),open=('open','first'),high=('high','max'),
                low=('low','min'),close=('close','last'),n=('timestamp','size'))
        # Only exact UTC-aligned complete groups.
        a=a.loc[(a.n==size)&(a.timestamp.astype('int64')==a.index.astype('int64'))]
        out=a[['timestamp','open','high','low','close']].reset_index(drop=True)
    if out.empty or len(out)>max_bars:
        raise ValueError('Cantidad de velas excede el límite visual; elegí una temporalidad mayor')
    return out.reset_index(drop=True)
