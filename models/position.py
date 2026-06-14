from dataclasses import dataclass
from typing import Optional


@dataclass
class Position:
    symbol: str
    side: str

    quantity: float

    entry_price: float
    real_entry: float

    tp: float
    sl: float

    entry_ts: int
    signal_price: float
    signal_ts: int
    
    leverage: int = 1
    is_testnet: bool = False

    signal_context: dict | None = None

    plan_max_hold_candles: int = 10
    entry_candle_ts: Optional[int] = None
    candles_in_trade: int = 1

    exit_order_id: Optional[int] = None

    # ========================
    # TRADE EVOLUTION
    # ========================

    current_pnl: float = 0.0

    mae: float | None = None
    mfe: float | None = None

    current_momentum: Optional[str] = None
    current_direction: Optional[str] = None
    current_trend: Optional[str] = None

    momentum_t1: Optional[str] = None
    direction_t1: Optional[str] = None
    pnl_t1: Optional[float] = None
    
    # ========================
    # AGGRESSIVE POST ANALYSIS
    # ========================

    micro_t1: Optional[str] = None
    direction_5m_t1: Optional[str] = None

    reclaimed_ema20_1m: bool = False
    reclaimed_ema34_1m: bool = False
    reclaimed_ema50_1m: bool = False

    lost_ema20_1m: bool = False
    lost_ema34_1m: bool = False
    lost_ema50_1m: bool = False

    dist_ema20_1m_pct: Optional[float] = None
    dist_ema34_1m_pct: Optional[float] = None
    dist_ema50_1m_pct: Optional[float] = None

    max_favorable_pct: Optional[float] = None
    max_adverse_pct: Optional[float] = None

    direction_5m_changed: bool = False
    direction_5m_after_entry: Optional[str] = None
    
    be_moved: bool = False
    
    strategy_mode: Optional[str] = None
    router_reason: Optional[str] = None
    
    