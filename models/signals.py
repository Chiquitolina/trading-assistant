from dataclasses import dataclass
from typing import Optional

from enums.trend import Trend
from enums.direction import Direction
from enums.momentum import Momentum


@dataclass
class Signal:
    symbol: str
    signal_price: float
    signal_ts: int

    trend: Trend
    direction: Direction
    momentum: Momentum

    momentum_prev1: Optional[Momentum] = None
    momentum_prev2: Optional[Momentum] = None
    momentum_sequence: list = None
    
    # =========================
    # SIGNAL TIMEFRAME IDENTITY
    # =========================
    main_tf: Optional[str] = None
    signal_context_tf: Optional[str] = None
    swing_lookback: Optional[int] = None

    # =========================
    # MICRO MOMENTUM CONTEXT
    # =========================
    micro: Optional[Momentum] = None

    # =========================
    # AGGRESSIVE CONTEXT
    # =========================
    atr_5m: Optional[float] = None
    atr_5m_pct: float = 0.0

    ema20_1m: Optional[float] = None
    ema34_1m: Optional[float] = None
    ema50_1m: Optional[float] = None

    # =========================
    # EMA CONTEXT - 5m
    # =========================
    ema20_5m: Optional[float] = None
    ema50_5m: Optional[float] = None
    ema99_5m: Optional[float] = None
    ema100_5m: Optional[float] = None

    dist_ema20_5m_pct: Optional[float] = None
    dist_ema50_5m_pct: Optional[float] = None
    dist_ema99_5m_pct: Optional[float] = None

    # =========================
    # EMA CONTEXT - 15m
    # =========================
    ema20_15m: Optional[float] = None
    ema50_15m: Optional[float] = None
    ema99_15m: Optional[float] = None

    dist_ema20_15m_pct: Optional[float] = None
    dist_ema50_15m_pct: Optional[float] = None
    dist_ema99_15m_pct: Optional[float] = None

    # =========================
    # EMA CONTEXT - 30m
    # =========================
    ema20_30m: Optional[float] = None
    ema50_30m: Optional[float] = None
    ema99_30m: Optional[float] = None

    dist_ema20_30m_pct: Optional[float] = None
    dist_ema50_30m_pct: Optional[float] = None
    dist_ema99_30m_pct: Optional[float] = None

    # =========================
    # EMA CONTEXT - 1h
    # =========================
    ema20_1h: Optional[float] = None
    ema50_1h: Optional[float] = None
    ema99_1h: Optional[float] = None

    dist_ema20_1h_pct: Optional[float] = None
    dist_ema50_1h_pct: Optional[float] = None
    dist_ema99_1h_pct: Optional[float] = None

    # =========================
    # EMA CONTEXT - 4h
    # =========================
    ema20_4h: Optional[float] = None
    ema50_4h: Optional[float] = None
    ema99_4h: Optional[float] = None

    dist_ema20_4h_pct: Optional[float] = None
    dist_ema50_4h_pct: Optional[float] = None
    dist_ema99_4h_pct: Optional[float] = None

    htf_bullish: Optional[bool] = None
    htf_bearish: Optional[bool] = None

    ema_alignment_bullish: Optional[bool] = None
    near_ema20_long: Optional[bool] = None
    near_ema20_short: Optional[bool] = None
    near_ema50_long: Optional[bool] = None
    
    # =========================
    # RECENT MOVE CONTEXT - 15m
    # =========================
    
    move_5_bars_pct: Optional[float] = None
    move_10_bars_pct: Optional[float] = None

    green_candles_last_10: Optional[int] = None
    red_candles_last_10: Optional[int] = None

    # =========================
    # LEGACY SWING CONTEXT
    # =========================
    swing_low: Optional[float] = None
    swing_high: Optional[float] = None

    near_swing_low: Optional[bool] = None
    near_swing_high: Optional[bool] = None
    
    # =========================
    # SWING CONTEXT - 5m
    # =========================
    swing_low_5m: Optional[float] = None
    swing_high_5m: Optional[float] = None

    dist_swing_low_5m_pct: Optional[float] = None
    dist_swing_high_5m_pct: Optional[float] = None

    near_swing_low_5m: Optional[bool] = None
    near_swing_high_5m: Optional[bool] = None

    # =========================
    # HTF SWING CONTEXT - 15m
    # =========================
    swing_low_15m: Optional[float] = None
    swing_high_15m: Optional[float] = None

    dist_swing_low_15m_pct: Optional[float] = None
    dist_swing_high_15m_pct: Optional[float] = None

    near_swing_low_15m: Optional[bool] = None
    near_swing_high_15m: Optional[bool] = None
    
    # =========================
    # SWING CONTEXT - 30m
    # =========================
    swing_low_30m: Optional[float] = None
    swing_high_30m: Optional[float] = None

    dist_swing_low_30m_pct: Optional[float] = None
    dist_swing_high_30m_pct: Optional[float] = None

    near_swing_low_30m: Optional[bool] = None
    near_swing_high_30m: Optional[bool] = None

    # =========================
    # HTF SWING CONTEXT - 1h
    # =========================
    swing_low_1h: Optional[float] = None
    swing_high_1h: Optional[float] = None

    dist_swing_low_1h_pct: Optional[float] = None
    dist_swing_high_1h_pct: Optional[float] = None

    near_swing_low_1h: Optional[bool] = None
    near_swing_high_1h: Optional[bool] = None

    # =========================
    # HTF SWING CONTEXT - 4h
    # =========================
    swing_low_4h: Optional[float] = None
    swing_high_4h: Optional[float] = None

    dist_swing_low_4h_pct: Optional[float] = None
    dist_swing_high_4h_pct: Optional[float] = None

    near_swing_low_4h: Optional[bool] = None
    near_swing_high_4h: Optional[bool] = None
    
    # =========================
    # LIQUIDITY CONTEXT
    # =========================
    quote_volume_24h: Optional[float] = None
    
    # =========================
    # BTC SWING CONTEXT
    # =========================
    btc_dist_swing_low_1h_pct: Optional[float] = None
    btc_dist_swing_high_1h_pct: Optional[float] = None
    btc_near_swing_low_1h: Optional[bool] = None
    btc_near_swing_high_1h: Optional[bool] = None

    btc_dist_swing_low_4h_pct: Optional[float] = None
    btc_dist_swing_high_4h_pct: Optional[float] = None
    btc_near_swing_low_4h: Optional[bool] = None
    btc_near_swing_high_4h: Optional[bool] = None

    btc_dist_swing_low_1d_pct: Optional[float] = None
    btc_dist_swing_high_1d_pct: Optional[float] = None
    btc_near_swing_low_1d: Optional[bool] = None
    btc_near_swing_high_1d: Optional[bool] = None