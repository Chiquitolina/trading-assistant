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

    signal_context: dict | None = None

    plan_max_hold_candles: int = 10
    entry_candle_ts: Optional[int] = None
    candles_in_trade: int = 1

    exit_order_id: Optional[int] = None

    # ========================
    # TRADE EVOLUTION
    # ========================

    current_pnl: float = 0.0

    mae: float = 0.0
    mfe: float = 0.0

    current_momentum: Optional[str] = None
    current_direction: Optional[str] = None
    current_trend: Optional[str] = None

    momentum_t1: Optional[str] = None
    direction_t1: Optional[str] = None
    pnl_t1: Optional[float] = None
    
    strategy_mode: Optional[str] = None
    router_reason: Optional[str] = None
    
    