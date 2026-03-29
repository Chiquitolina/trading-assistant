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

        last_candle = candles_15m[-1]
        signal_price = last_candle["close"]
        signal_ts = last_candle["timestamp"]

        # 🧪 DEBUG: timestamps y últimas velas
        candles_5m = self.buffer.get_candles("5m")
        if candles_5m:
            last_5m_ts = candles_5m[-1]["timestamp"]
        else:
            last_5m_ts = None

        print("\033[95m[DEBUG]\033[0m ⏱ TIMESTAMPS")
        print(f"15m signal_ts : {signal_ts}")
        print(f"5m last_ts    : {last_5m_ts}\n")

        print("\033[95m[DEBUG]\033[0m 🕯️ LAST CANDLES")
        print("15m last 2:")
        print(pd.DataFrame(candles_15m).tail(2)[["timestamp", "close"]])

        if candles_5m:
            print("\n5m last 3:")
            print(pd.DataFrame(candles_5m).tail(3)[["timestamp", "close"]])
        print()

        # 🔒 evitar duplicados
        if signal_ts == self.last_signal_ts:
            return None
        self.last_signal_ts = signal_ts

        trend     = self.get_trend()
        direction = self.get_direction()
        momentum  = self.get_momentum()

        if self.debug:
            print("\033[94m[SIGNALS LAYER]\033[0m 📷  Snapshot (on 15m close)")
            print(f"1h trend     : {trend}")
            print(f"15m direction: {direction}")
            print(f"5m momentum  : {momentum}\n")

        # ==========================
        # LONG SIGNAL
        # ==========================
        if long_setup(trend, direction, momentum):
            print("\033[94m[SIGNALS LAYER]\033[0m 💡 SIGNAL GENERATED: LONG")
            return {
                "side": "LONG",
                "signal_price": round(signal_price, 2),
                "signal_ts": signal_ts,
                "trend": trend,
                "direction": direction,
                "momentum": momentum
            }

        # ==========================
        # SHORT SIGNAL
        # ==========================
        if short_setup(trend, direction, momentum):
            print("\033[94m[SIGNALS LAYER]\033[0m 💡 SIGNAL GENERATED: SHORT")
            return {
                "side": "SHORT",
                "signal_price": round(signal_price, 2),
                "signal_ts": signal_ts,
                "trend": trend,
                "direction": direction,
                "momentum": momentum
            }

        return None