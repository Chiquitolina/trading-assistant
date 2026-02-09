# strategy/entry_engine.py
import pandas as pd

from signals.indicators.atr import add_atr
from signals.strategy.filters import min_expected_tp_ok
from signals.strategy.risk import compute_levels
from engine.live.strategy.trade_plan import TradePlan
from config.strategies.v1 import LONG, SHORT


class EntryEngine:
    def __init__(self, buffer, debug=True):
        self.buffer = buffer
        self.debug = debug

    def generate_entry(self, side: str):

        if side not in ("LONG", "SHORT"):
            return None

        # ==========================
        # DATA (igual que backtest)
        # ==========================
        df_15m = pd.DataFrame(self.buffer.get_candles("15m"))
        if len(df_15m) < 20:
            return None

        df_15m = add_atr(df_15m, period=14)

        signal_candle = df_15m.iloc[-2]   # vela cerrada
        entry_candle  = df_15m.iloc[-1]   # vela actual

        entry = entry_candle["open"]
        atr = signal_candle["atr"]

        if pd.isna(atr):
            return None

        cfg = LONG if side == "LONG" else SHORT

        # ==========================
        # FILTER (idéntico)
        # ==========================
        if not min_expected_tp_ok(
            entry,
            atr,
            cfg["tp_mult"],
            cfg["min_tp"]
        ):
            if self.debug:
                print("⛔ Entry descartado: TP esperado insuficiente")
            return None

        # ==========================
        # LEVELS (idéntico)
        # ==========================
        sl, tp, sl_pct, tp_pct = compute_levels(
            side=side,
            entry=entry,
            atr=atr,
            cfg=cfg
        )

        plan = TradePlan(
            side=side,
            entry=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            sl_pct=round(sl_pct, 3),
            tp_pct=round(tp_pct, 3),
            atr=round(atr, 2),
            timestamp=self.buffer.last_timestamp(),  # 👈 ACÁ
            reason="strategy_v1"
        )

        if self.debug:
            print('\n')
            print("📥 TRADE PLAN")
            print('\n')
            print(plan.pretty())            
            print('\n')

        return plan
