from dataclasses import dataclass, field


@dataclass
class SignalCompressionSnapshot:
    symbol: str
    timestamp: int

    trend_15m: str | None = None
    trend_1h: str | None = None
    trend_4h: str | None = None

    adx_1h: float | None = None
    rsi_1h: float | None = None
    macd_hist_1h: float | None = None

    volume_ratio_15m: float | None = None
    volume_ratio_1h: float | None = None
    
    atr_ratio_15m: float | None = None
    range_ratio_15m: float | None = None

    compression_score: int | None = None
    compression_high: float | None = None
    compression_low: float | None = None

    breakout_side: str | None = None
    breakout_score: int | None = None

    move_3bars_pct: float | None = None
    dist_ema20_1h_pct: float | None = None
    candle_progress_pct: float | None = None

    btc_corr_7d: float | None = None
    btc_corr_30d: float | None = None

    beta_vs_btc_7d: float | None = None
    beta_vs_btc_30d: float | None = None

    r2_vs_btc_7d: float | None = None
    r2_vs_btc_30d: float | None = None

    vol_ratio_vs_btc_7d: float | None = None

    outperformance_7d: float | None = None
    outperformance_30d: float | None = None

    tags: list[str] = field(default_factory=list)