import pandas as pd
from signals.indicators.trend import trend_bias
from signals.indicators.momentum import momentum_5m
from signals.indicators.direction import trade_direction
from signals.strategy.entries import long_setup, short_setup


class SignalEngine:
    def __init__(self, buffer, debug=True):
        self.buffer = buffer
        self.debug = debug
        self.last_signal_ts = None
        self.momentum_history = []

    def get_trend(self):
        df = pd.DataFrame(self.buffer.get_candles("1h"))
        if len(df) < 20:
            return "neutral"

        df = trend_bias(df)
        return df.iloc[-1]["trend"]

    def get_direction(self):
        df = pd.DataFrame(self.buffer.get_candles("15m"))
        if len(df) < 10:
            return None

        result = trade_direction(df)

        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["direction"]

        return result

    def get_momentum(self):
        df = pd.DataFrame(self.buffer.get_candles("5m"))
        if len(df) < 2:
            return "none"

        result = momentum_5m(df)

        if isinstance(result, pd.DataFrame):
            return result.iloc[-1]["momentum"]

        return result

    def generate_signal(self):
        candles_15m = self.buffer.get_candles("15m")
        if not candles_15m:
            return None

        last_candle = candles_15m[-1]
        signal_price = last_candle["close"]

        tf_ms = 15 * 60 * 1000
        signal_ts = last_candle["timestamp"] + tf_ms - 1

        candles_5m = self.buffer.get_candles("5m")
        last_5m_ts = candles_5m[-1]["timestamp"] if candles_5m else None

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

        print(f"\033[95m[SIGNAL DEBUG]\033[0m signal_ts={signal_ts} | last_signal_ts={self.last_signal_ts}")

        if signal_ts == self.last_signal_ts:
            return None
        self.last_signal_ts = signal_ts

        df_15m = pd.DataFrame(candles_15m)
        df_5m = pd.DataFrame(candles_5m) if candles_5m else pd.DataFrame()

        print("\033[95m[LIVE DEBUG]\033[0m 15m candle used:")
        if not df_15m.empty:
            print(df_15m.tail(1)[["timestamp", "open", "high", "low", "close"]])

        print("\033[95m[LIVE DEBUG]\033[0m last 5 candles of 5m used for momentum:")
        if not df_5m.empty:
            print(df_5m.tail(5)[["timestamp", "open", "high", "low", "close"]])
            print(f"\033[95m[LIVE DEBUG]\033[0m last 5m used ts: {df_5m.tail(1).iloc[0]['timestamp']}")
        else:
            print("No 5m candles available")

        trend = self.get_trend()
        direction = self.get_direction()
        momentum = self.get_momentum()
        
        # 🧠 guardar historial de momentum
        self.momentum_history.append(momentum)

        # mantener tamaño chico (opcional pero recomendado)
        if len(self.momentum_history) > 10:
            self.momentum_history.pop(0)
            
        prev1 = self.momentum_history[-2] if len(self.momentum_history) >= 2 else None
        prev2 = self.momentum_history[-3] if len(self.momentum_history) >= 3 else None

        print("\033[95m[LIVE DEBUG]\033[0m indicator result:")
        print(f"trend     : {trend}")
        print(f"direction : {direction}")
        print(f"momentum  : {momentum}\n")
        
        print(f"prev1     : {prev1}")
        print(f"prev2     : {prev2}")

        long_ok = long_setup(trend, direction, momentum)
        short_ok = short_setup(trend, direction, momentum)

        if self.debug:
            print("\033[94m[SIGNALS LAYER]\033[0m 📷  Snapshot (on 15m close)")
            print(f"1h trend     : {trend}")
            print(f"15m direction: {direction}")
            print(f"5m momentum  : {momentum}")
            print(f"long_ok      : {long_ok}")
            print(f"short_ok     : {short_ok}\n")

        side = "NONE"

        if long_ok:
            side = "LONG"
            print("\033[94m[SIGNALS LAYER]\033[0m 💡 SIGNAL GENERATED: LONG")
        elif short_ok:
            side = "SHORT"
            print("\033[94m[SIGNALS LAYER]\033[0m 💡 SIGNAL GENERATED: SHORT")
        else:
            print("\033[94m[SIGNALS LAYER]\033[0m 🚫 NO SIGNAL")
            
        return {
            "side": side,
            "signal_price": round(signal_price, 2),
            "signal_ts": signal_ts,
            "trend": trend,
            "direction": direction,
            "momentum": momentum,

            # 🔥 NUEVO (research)
            "momentum_prev1": prev1,
            "momentum_prev2": prev2,

            # opcional (MUY útil)
            "momentum_sequence": [prev2, prev1, momentum],

            "long_ok": long_ok,
            "short_ok": short_ok,
        }