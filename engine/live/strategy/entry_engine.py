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

        signal_candle = df_15m.iloc[-2]   # vela cerrada (donde nace la señal)
        entry_candle  = df_15m.iloc[-1]   # vela actual (ejecución)

        # 🔹 PRECIO DE SEÑAL
        signal_price = signal_candle["close"]
        signal_ts = signal_candle["timestamp"]

        # 🔹 PRECIO DE ENTRADA REAL
        entry = entry_candle["open"]
        atr = signal_candle["atr"]

        if pd.isna(atr):
            return None

        cfg = LONG if side == "LONG" else SHORT

        # ==========================
        ok, expected_tp_pct = min_expected_tp_ok(
            entry,
            atr,
            cfg["tp_mult"],
            cfg["min_tp"]
        )

        if not ok:
            if self.debug:
                print(f"⛔ Entry descartado: TP esperado insuficiente ({expected_tp_pct:.2f}% < {cfg['min_tp']}%)")
            return None

        # ==========================
        # LEVELS
        # ==========================
        sl, tp, sl_pct, tp_pct = compute_levels(
            side=side,
            entry=entry,
            atr=atr,
            cfg=cfg
        )

        plan = TradePlan(
            symbol="BTCUSDT",
            quantity=0.001,

            side=side,
            entry=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            sl_pct=round(sl_pct, 3),
            tp_pct=round(tp_pct, 3),
            atr=round(atr, 2),

            # ⏱ timestamp de ejecución
            timestamp=entry_candle["timestamp"],

            reason="strategy_v1",

            # 🆕 DATA DE SEÑAL
            signal_price=round(signal_price, 2),
            signal_ts=signal_ts
        )

        if self.debug:
            print(plan.pretty())

        return plan