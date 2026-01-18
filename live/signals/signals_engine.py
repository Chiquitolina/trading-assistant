import pandas as pd
from common.indicators.trend import trend_bias
from common.indicators.momentum import momentum_5m
from indicators.direction import trade_direction


class SignalEngine:
    def __init__(self, buffer, debug=True):
        self.buffer = buffer
        self.debug = debug

        # 🔒 Evitar evaluar dos veces la misma vela
        self.last_signal_ts = None

    # -------------------------
    # INDICATORS
    # -------------------------
    def get_trend(self):
        df = pd.DataFrame(self.buffer.get_candles("1h"))
        if len(df) < 20:
            return "neutral"
        return trend_bias(df)

    def get_direction(self):
        df = pd.DataFrame(self.buffer.get_candles("15m"))
        if len(df) < 10:
            return None
        return trade_direction(df)

    def get_momentum(self):
        df = pd.DataFrame(self.buffer.get_candles("5m"))
        if len(df) < 2:
            return "none"
        return momentum_5m(df)

    # -------------------------
    # SIGNAL LOGIC
    # -------------------------
    def generate_signal(self):
        candles_5m = self.buffer.get_candles("5m")
        if not candles_5m:
            return None

        last_5m_ts = candles_5m[-1]["timestamp"]

        # ⛔ Ya evaluado
        if last_5m_ts == self.last_signal_ts:
            return None

        self.last_signal_ts = last_5m_ts

        # ---------- TOMAR DATOS ----------
        trend = self.get_trend()
        direction = self.get_direction()
        momentum = self.get_momentum()

        # ---------- DEBUG ----------
        if self.debug:
            print("\n🕯️ Snapshot (on 5m close)")
            print(f"1h trend     : {trend}")
            print(f"15m direction: {direction}")
            print(f"5m momentum  : {momentum}\n")

        # ---------- GENERAR SEÑAL ----------
        if trend == "bullish" and direction == "up" and momentum in ("impulse_up", "breakout_up"):
            print("💡 SIGNAL GENERATED: LONG")
            return "LONG"

        if trend == "bearish" and direction == "down" and momentum in ("impulse_down", "breakout_down"):
            print("💡 SIGNAL GENERATED: SHORT")
            return "SHORT"

        return None
