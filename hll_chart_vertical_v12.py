"""Presentation-only vertical viewport for the HLL BTC research chart."""
import numpy as np
import pandas as pd

def price_axis_window(plot_bars:pd.DataFrame, pad_ratio:float=.12) -> tuple[float,float]:
    """Local OHLC extrema + proportional room; ignores distant HLL overlays.

    Uses only bars already authorized by the historical cutoff. Never clips
    actual candle wicks; the padding also prevents flattened price action.
    """
    lows=pd.to_numeric(plot_bars['low'],errors='raise').to_numpy(dtype=float)
    highs=pd.to_numeric(plot_bars['high'],errors='raise').to_numpy(dtype=float)
    if not len(lows) or not np.isfinite(lows).all() or not np.isfinite(highs).all():
        raise ValueError('Velas inválidas para zoom vertical')
    lo,hi=float(lows.min()),float(highs.max())
    if lo<=0 or hi<lo or not (0<=pad_ratio<=1):
        raise ValueError('Extremos OHLC inválidos para zoom vertical')
    margin=max((hi-lo)*pad_ratio, (hi+lo)*.0005)
    return lo-margin,hi+margin


def zone_in_price_window(zone:dict,price_window:tuple[float,float]) -> bool:
    """Only draw zone traces intersecting the local viewport; never re-rank."""
    return float(zone['high'])>=price_window[0] and float(zone['low'])<=price_window[1]


