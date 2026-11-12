#!/usr/bin/env python3
"""Lectura verificada y consultas as-of para Multi-Swing BTC 60D.

Solo lectura; no modifica HLL, REACTION, LIVE ni artefactos congelados.
La identidad física de un pivote NO implica que sus otras temporalidades
estuvieran confirmadas en el mismo instante.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

BASE_REL = Path('reports/reaction_lab/btc_multiswing_60d_v1')
FREEZE_REL = BASE_REL / 'btc_sep09_nov08_60d_v1'
CATALOG_REL = BASE_REL / 'btc_sep09_nov08_swing_catalog_6tf_v1'
TF_MINUTES = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240}
TF_COLORS = {
    '1m': '#91a3b5', '5m': '#67a7ec', '15m': '#bc97ff',
    '30m': '#e2a44a', '1h': '#34bca0', '4h': '#e77f83',
}
M = 60_000
DAY = 86_400_000
REQUIRED_SWING_COLUMNS = {
    'pivot_id', 'timeframe', 'side', 'price', 'pivot_ts', 'available_ts',
    'physical_identity_state', 'physical_id', 'physical_minute_ts',
}


def iso(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def ms(iso_s: str) -> int:
    return int(datetime.fromisoformat(iso_s.replace('Z', '+00:00')).timestamp() * 1000)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _verified_csv(path: Path, expected_sha: str) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f'Archivo congelado no encontrado: {path}')
    if digest(path) != expected_sha:
        raise ValueError(f'HASH_MISMATCH: {path}')
    return pd.read_csv(path)


def _validate_minute(frame: pd.DataFrame, start: int, end: int, source: str) -> pd.DataFrame:
    fields = {'timestamp', 'open', 'high', 'low', 'close'}
    if not fields.issubset(frame.columns):
        raise ValueError(f'INVALID_OHLC_SCHEMA: {source}')
    out = frame.copy()
    for column in fields:
        out[column] = pd.to_numeric(out[column], errors='coerce')
    if out[list(fields)].isna().any().any() or not all(
        math.isfinite(float(x)) for x in out[list(fields)].to_numpy().ravel()
    ):
        raise ValueError(f'INVALID_OHLC_NUMERIC: {source}')
    t = out['timestamp'].astype('int64')
    expected = (end - start) // M
    if (len(out) != expected or t.iloc[0] != start or t.iloc[-1] != end-M
        or (t.diff().iloc[1:] != M).any()):
        raise ValueError(f'NONCONTIGUOUS_1M: {source}')
    if ((out[['open','high','low','close']] <= 0).any().any()
        or (out['high'] < out[['open','close','low']].max(axis=1)).any()
        or (out['low'] > out[['open','close','high']].min(axis=1)).any()):
        raise ValueError(f'INVALID_OHLC: {source}')
    return out


def read_snapshot(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Lee el freeze original y catálogo, validando hashes y cobertura.

    No utiliza physical_extrema_groups.csv: sus miembros son retrospectivos.
    Las agrupaciones se reconstruyen siempre después de filtrar available_ts.
    """
    frozen = Path(root) / FREEZE_REL
    cat = Path(root) / CATALOG_REL
    source_manifest = json.loads((frozen / 'manifest.json').read_text(encoding='utf-8'))
    catalog_manifest = json.loads((cat / 'manifest.json').read_text(encoding='utf-8'))
    if source_manifest.get('integrity') != 'CONTIGUOUS_1M_VERIFIED':
        raise ValueError('FROZEN_INTEGRITY_NOT_VERIFIED')
    if source_manifest.get('hll_30m_parity', {}).get('status') != 'PARITY_OK':
        raise ValueError('FROZEN_HLL_PARITY_NOT_OK')
    if catalog_manifest.get('version') != 'btc-multiswing-60d-catalog-v1.1-6tf':
        raise ValueError('INVALID_CATALOG_VERSION')
    src_shas = source_manifest['files_sha256']
    if (catalog_manifest.get('source_csv_sha256') != src_shas['BTCUSDT_1m.csv']
        or catalog_manifest.get('source_warmup_sha256') != src_shas['BTCUSDT_1m_warmup.csv']):
        raise ValueError('CATALOG_FREEZE_SOURCE_MISMATCH')
    if set(catalog_manifest.get('timeframes', [])) != set(TF_MINUTES):
        raise ValueError('CATALOG_NOT_SIX_TF')
    start = ms(source_manifest['main_start_utc'])
    end = ms(source_manifest['main_end_exclusive_utc'])
    warm_start = ms(source_manifest['warmup_start_utc'])
    if (start != ms('2026-09-09T01:00:00Z') or end != ms('2026-11-08T01:00:00Z')
        or ms(source_manifest['warmup_end_exclusive_utc']) != start):
        raise ValueError('FREEZE_WINDOW_MISMATCH')
    main = _validate_minute(_verified_csv(frozen/'BTCUSDT_1m.csv', src_shas['BTCUSDT_1m.csv']), start, end, 'MAIN')
    warm = _validate_minute(_verified_csv(frozen/'BTCUSDT_1m_warmup.csv', src_shas['BTCUSDT_1m_warmup.csv']), warm_start, start, 'WARMUP')
    raw = _verified_csv(cat/'confirmed_swings.csv', catalog_manifest['files_sha256']['confirmed_swings.csv'])
    if not REQUIRED_SWING_COLUMNS.issubset(raw.columns):
        raise ValueError('CATALOG_SWING_SCHEMA_MISMATCH')
    if len(raw) != int(catalog_manifest['pivot_count']):
        raise ValueError('CATALOG_COUNT_MISMATCH')
    catalog = raw.copy()
    for col in ('pivot_ts', 'available_ts', 'price'):
        catalog[col] = pd.to_numeric(catalog[col], errors='coerce')
    if catalog[['pivot_ts', 'available_ts', 'price']].isna().any().any():
        raise ValueError('INVALID_PIVOT_NUMERIC')
    for col in ('pivot_ts', 'available_ts'):
        catalog[col] = catalog[col].astype('int64')
    if ((catalog['pivot_ts'] < start) | (catalog['pivot_ts'] >= end)
        | (catalog['available_ts'] <= catalog['pivot_ts'])
        | (catalog['available_ts'] > end)).any():
        raise ValueError('INVALID_PIVOT_TIMELINE')
    if ((catalog['price'] <= 0) | (~catalog['price'].map(math.isfinite))).any():
        raise ValueError('INVALID_PIVOT_PRICE')
    if (catalog['pivot_id'].isna().any() or catalog['pivot_id'].duplicated().any()
        or not catalog['timeframe'].isin(TF_MINUTES).all()
        or not catalog['side'].isin(['HIGH','LOW']).all()
        or not catalog['physical_identity_state'].isin(['VERIFIED','TIED_EXTREMA']).all()):
        raise ValueError('INVALID_PIVOT_CATEGORIES_OR_IDS')
    verified = catalog['physical_identity_state'] == 'VERIFIED'
    if (catalog.loc[verified, 'physical_id'].isna().any()
        or (catalog.loc[verified, 'physical_id'].astype(str).str.strip() == '').any()):
        raise ValueError('VERIFIED_MISSING_PHYSICAL_ID')
    catalog['physical_id'] = catalog['physical_id'].fillna('').astype(str)
    catalog['physical_minute_ts'] = pd.to_numeric(catalog['physical_minute_ts'], errors='coerce')
    if catalog.loc[verified, 'physical_minute_ts'].isna().any():
        raise ValueError('VERIFIED_MISSING_PHYSICAL_TS')
    stamps = {'start':start, 'end':end, 'warmup_start':warm_start,
              'source_sha256':src_shas['BTCUSDT_1m.csv'],
              'catalog_sha256':catalog_manifest['files_sha256']['confirmed_swings.csv'],
              'source_hll_parity':source_manifest['hll_30m_parity']['status']}
    full = pd.concat([warm,main],ignore_index=True)
    return full, catalog, stamps


def _cache_signature(root: Path) -> tuple:
    """Invalidate process cache when any frozen file changes."""
    files = [Path(root)/FREEZE_REL/name for name in ('manifest.json', 'BTCUSDT_1m.csv', 'BTCUSDT_1m_warmup.csv')]
    files += [Path(root)/CATALOG_REL/name for name in ('manifest.json', 'confirmed_swings.csv')]
    return tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in files)


@lru_cache(maxsize=2)
def _cached_read(root_str: str, signature: tuple):
    return read_snapshot(Path(root_str))


def load_snapshot(root: Path):
    path = Path(root).resolve()
    return _cached_read(str(path), _cache_signature(path))


def asof_rows(catalog: pd.DataFrame, cutoff: int, timeframes: tuple[str, ...], side: str = 'BOTH') -> pd.DataFrame:
    if side not in ('BOTH','HIGH','LOW'):
        raise ValueError('INVALID_SIDE')
    if not timeframes or len(set(timeframes)) != len(timeframes) or any(tf not in TF_MINUTES for tf in timeframes):
        raise ValueError('INVALID_TIMEFRAME_SELECTION')
    out = catalog[(catalog['available_ts'] <= int(cutoff)) & catalog['timeframe'].isin(timeframes)].copy()
    if side != 'BOTH':
        out = out[out['side'] == side].copy()
    return out


def group_asof(rows: pd.DataFrame, *, collapse: bool, multi_only: bool = False) -> pd.DataFrame:
    """Causal physical groups, vectorized without changing V1 grouping rules.

    V3.1.4 performance: group metadata is computed in columnar batches instead
    of one Python loop and four Python mapping lambdas for each physical group.
    TIED_EXTREMA keep their own pivot ID, never a fabricated physical identity.
    Only already-confirmed rows supplied by the caller are used.
    """
    if rows.empty:
        return pd.DataFrame(columns=[*rows.columns, 'representations_asof', 'tf_set_asof',
                                     'first_available_asof', 'last_available_asof', 'plot_ts', 'group_key'])
    frame = rows.copy()
    known = frame['physical_identity_state'].eq('VERIFIED') & frame['physical_id'].ne('')
    frame['group_key'] = frame['pivot_id'].astype(str)
    frame.loc[known, 'group_key'] = frame.loc[known, 'physical_id'].astype(str)

    # Preserve the exact physical identity conflict guard from the reference.
    # Count all side values (including null), and ignore nulls for the original
    # physical-minute/price nunique checks.
    valid = frame.loc[known]
    if not valid.empty:
        verified_groups = valid.groupby('group_key', sort=False)
        if ((verified_groups['side'].nunique(dropna=False) != 1).any()
            or (verified_groups['physical_minute_ts'].nunique(dropna=True) != 1).any()
            or (verified_groups['price'].nunique(dropna=True) != 1).any()):
            raise ValueError('PHYSICAL_GROUP_CONFLICT')

    groups = frame.groupby('group_key', sort=False)
    counts = groups.size()
    first = groups['available_ts'].min()
    last = groups['available_ts'].max()
    # Unique timeframe names in canonical minute order, not lexical order.
    tf_text = groups['timeframe'].agg(
        lambda values: ','.join(sorted(set(values), key=lambda tf: TF_MINUTES[tf]))
    )
    frame['representations_asof'] = frame['group_key'].map(counts)
    frame['tf_set_asof'] = frame['group_key'].map(tf_text)
    frame['first_available_asof'] = frame['group_key'].map(first)
    frame['last_available_asof'] = frame['group_key'].map(last)
    frame['plot_ts'] = frame['pivot_ts'].astype('int64')
    frame.loc[known, 'plot_ts'] = frame.loc[known, 'physical_minute_ts'].astype('int64')
    if collapse:
        frame['_tf_rank'] = frame['timeframe'].map(TF_MINUTES)
        frame = (frame.sort_values(['group_key', '_tf_rank', 'available_ts'])
                 .drop_duplicates('group_key', keep='last')
                 .drop(columns='_tf_rank'))
    if multi_only:
        frame = frame[(frame['physical_identity_state'] == 'VERIFIED') &
                      (frame['representations_asof'] >= 2)]
    return frame.sort_values(['plot_ts', 'side', 'timeframe']).reset_index(drop=True)


def window_evidence_stats(known: pd.DataFrame, start: int, end: int, *,
                          multi_only: bool = False) -> dict[str, int]:
    """Metricas PRE de extremos *dentro* de la ventana de exploracion.

    `known` ya esta filtrado por available_ts <= corte, timeframe y lado.
    La agrupacion por identidad se calcula as-of *antes* del recorte espacial,
    para que confirmaciones de un mismo extremo no se pierdan en el borde.
    La ventana se aplica al minuto fisico verificado o pivot_ts ambiguo,
    usando el mismo plot_ts que el grafico. No lee resultados POST.
    """
    if int(end) <= int(start):
        raise ValueError('INVALID_EVIDENCE_WINDOW')
    reps = group_asof(known, collapse=False, multi_only=multi_only)
    reps = reps[(reps['plot_ts'] >= int(start)) &
                (reps['plot_ts'] < int(end))]
    verified = reps[reps['physical_identity_state'].eq('VERIFIED')]
    shared = verified.groupby('physical_id')['timeframe'].nunique()
    return {
        'representations': int(len(reps)),
        'verified_extrema': int(verified['physical_id'].nunique()),
        'multi_tf_extrema': int((shared >= 2).sum()),
        'ambiguous_representations': int(reps['physical_identity_state'].ne('VERIFIED').sum()),
    }


def aggregate_closed(full: pd.DataFrame, tf: str, start: int, end: int) -> pd.DataFrame:
    """Render only complete, UTC-aligned bars in [start,end). No partial TF."""
    if tf not in TF_MINUTES or int(end) <= int(start):
        raise ValueError('INVALID_CHART_INTERVAL')
    width = TF_MINUTES[tf]
    step = width * M
    frame = full[['timestamp','open','high','low','close']]
    if width > 1:
        # Input is frozen contiguous 1m across warmup and main. UTC epoch bins.
        indexed = frame.set_index(pd.to_datetime(frame['timestamp'],unit='ms',utc=True))
        res = indexed.resample(f'{width}min',origin='epoch',label='left',closed='left')
        sampled = res.agg({'open':'first','high':'max','low':'min','close':'last','timestamp':'count'})
        sampled = sampled.loc[sampled['timestamp'] == width]
        sampled['timestamp'] = sampled.index.asi8 // 1_000_000
        frame = sampled.reset_index(drop=True)
    # Permit a candle that overlaps the left boundary; forbid any candle
    # that is not fully closed by end (especially 4h at 01:00 UTC).
    result = frame.loc[(frame.timestamp + step > start) & (frame.timestamp + step <= end)].copy()
    return result.sort_values('timestamp').reset_index(drop=True)


def chart_window(end: int, start_limit: int, days: int) -> tuple[int,int]:
    if days not in (1,3,7,14,30,60):
        raise ValueError('INVALID_WINDOW_DAYS')
    return max(int(start_limit), int(end)-days*DAY), int(end)


def selected_inspection_window(row: pd.Series, before_hours: int, after_hours: int,
                               main_start: int, main_end: int, reveal_post: bool) -> tuple[int,int,int]:
    if before_hours not in (2,4,8,12,24,48) or after_hours not in (2,4,8,12,24):
        raise ValueError('INVALID_INSPECTION_HOURS')
    available = int(row['last_available_asof'])
    if available > main_end or available <= main_start:
        raise ValueError('INVALID_SELECTION_AVAILABILITY')
    # Ensure original pivot remains on plot even for long confirmation (4h).
    start = max(main_start, min(available-before_hours*3_600_000, int(row['plot_ts'])-3_600_000))
    end = min(main_end, available+(after_hours*3_600_000 if reveal_post else 0))
    return start,end,available
