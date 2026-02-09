import pandas as pd
from signals.indicators.trend import trend_bias
from signals.indicators.momentum import momentum_5m
from signals.indicators.direction import trade_direction
from signals.strategy.entries import long_setup, short_setup


class SignalEngine:
    def __init__(self, buffer, debug=True):
        self.buffer = buffer
        self.debug = debug

        # 🔒 Evitar evaluar dos veces la misma vela
        self.last_signal_ts = None

    # -------------------------
    # INDICATORS (LIVE SAFE)
    # -------------------------
    def get_trend(self):
        df = pd.DataFrame(self.buffer.get_candles("1h"))
        if len(df) < 20:
            return "neutral"

        df = trend_bias(df)

        # 🔥 SOLO el último valor
        return df.iloc[-1]["trend"]


    def get_direction(self):
        df = pd.DataFrame(self.buffer.get_candles("15m"))
        if len(df) < 10:
            return None

        result = trade_direction(df)

        # 🔒 por si devuelve DF
        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["direction"]

        return result


    def get_momentum(self):
        df = pd.DataFrame(self.buffer.get_candles("5m"))
        if len(df) < 2:
            return "none"

        result = momentum_5m(df)

        # 🔒 por si devuelve DF
        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["momentum"]

        return result


    # -------------------------
    # SIGNAL LOGIC
    # -------------------------
    def generate_signal(self):

        candles_15m = self.buffer.get_candles("15m")
        if not candles_15m:
            return None

        last_15m_ts = candles_15m[-1]["timestamp"]

        if last_15m_ts == self.last_signal_ts:
            return None

        self.last_signal_ts = last_15m_ts

        trend     = self.get_trend()
        direction = self.get_direction()
        momentum  = self.get_momentum()

        if self.debug:
            print("\n📷  Snapshot (on 15m close)")
            print(f"1h trend     : {trend}")
            print(f"15m direction: {direction}")
            print(f"5m momentum  : {momentum}\n")

        if long_setup(trend, direction, momentum):
            print("💡 SIGNAL GENERATED: LONG")
            return "LONG"

        if short_setup(trend, direction, momentum):
            print("💡 SIGNAL GENERATED: SHORT")
            return "SHORT"

        return None
