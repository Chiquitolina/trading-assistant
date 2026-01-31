from collections import deque
from datetime import datetime

class DataBuffer:
    def __init__(self, maxlen=300):
        
        self.new_closed_tf = None

        self.buffers = {
            "5m": deque(maxlen=maxlen),
            "15m": deque(maxlen=maxlen),
            "1h": deque(maxlen=maxlen),
        }

        # ÚLTIMO CLOSE TIME procesado por TF
        self.last_close_time = {
            "5m": None,
            "15m": None,
            "1h": None,
        }

    def on_ws_message(self, msg):
        # Solo klines de futures
        if msg.get("e") != "continuous_kline":
            return

        k = msg["k"]
        tf = k["i"]

        if tf not in self.buffers:
            return

    # ⛔ ignorar velas abiertas
        if not k["x"]:
            return

        close_time = k["T"]

    # 🔒 evitar duplicados
        if close_time == self.last_close_time[tf]:
            return

        candle = {
            "timestamp": close_time,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "closed_at": datetime.utcfromtimestamp(close_time / 1000),
        }

        self.buffers[tf].append(candle)
        self.last_close_time[tf] = close_time

    # 🔔 EVENTO: se cerró una vela de este timeframe
        self.new_closed_tf = tf

        print(f"🕯️ STORED [{tf}] {candle['close']}")


    def get_candles(self, tf):
        return list(self.buffers.get(tf, []))

    def load_historical(self, tf, df):
        if tf not in self.buffers:
            return

        for _, row in df.iterrows():
            ts = row["timestamp"]

            # 🔥 normalizar a epoch ms
            if hasattr(ts, "timestamp"):  # pandas.Timestamp
                close_time = int(ts.timestamp() * 1000)
            else:
                close_time = int(ts)

            if close_time == self.last_close_time[tf]:
                continue

            candle = {
                "timestamp": close_time,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "closed_at": datetime.utcfromtimestamp(close_time / 1000),
            }

            self.buffers[tf].append(candle)
            self.last_close_time[tf] = close_time

        print(f"📦 Loaded {len(df)} historical candles for {tf}")

