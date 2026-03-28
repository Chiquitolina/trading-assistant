from collections import deque
from datetime import datetime


class DataBuffer:
    def __init__(self, maxlen=300):

        # 🔔 timeframes que cerraron vela (NO se pisan)
        self.closed_tfs = set()

        self._last_price = None
        self._last_timestamp = None

        self.buffers = {
            "5m": deque(maxlen=maxlen),
            "15m": deque(maxlen=maxlen),
            "1h": deque(maxlen=maxlen),
        }

        # último close procesado por TF
        self.last_close_time = {
            "5m": None,
            "15m": None,
            "1h": None,
        }

    # ==========================================
    # WS MESSAGE
    # ==========================================
    def on_ws_message(self, msg):
        # solo klines de futures
        if msg.get("e") != "continuous_kline":
            return

        k = msg["k"]
        tf = k["i"]

        # 🔥 último precio SIEMPRE actualizado
        self._last_price = float(k["c"])
        self._last_timestamp = int(k["T"])

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

        # ✅ registrar cierre de TF (NO se pisa)
        self.closed_tfs.add(tf)

        print(f"\033[94m[DATA LAYER]\033[0m🕯️ STORED [{tf}] {candle['close']}")

    # ==========================================
    # EVENT CONSUMER
    # ==========================================
    def consume_closed_tf(self, tf: str) -> bool:
        """
        Devuelve True una sola vez por cada cierre de vela.
        Evita perder eventos entre timeframes.
        """
        if tf in self.closed_tfs:
            self.closed_tfs.remove(tf)
            return True
        return False

    # ==========================================
    # GETTERS
    # ==========================================
    def last_price(self):
        return self._last_price

    def last_timestamp(self):
        return self._last_timestamp

    def last_closed_candle(self, tf: str):
        if not self.buffers[tf]:
            return None
        return self.buffers[tf][-1]

    def get_candles(self, tf):
        return list(self.buffers.get(tf, []))

    # ==========================================
    # HISTORICAL LOAD
    # ==========================================
    def load_historical(self, tf, df):
        if tf not in self.buffers:
            return

        for _, row in df.iterrows():
            ts = row["timestamp"]

            # normalizar timestamp a epoch ms
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

        print(f"\033[94m[DATA LAYER]\033[0m 📦 Loaded {len(df)} historical candles for {tf}")

