from collections import deque
from datetime import datetime


class DataBuffer:
    def __init__(self, maxlen=300):

        self.closed_tfs = set()

        self._last_price = None
        self._last_timestamp = None

        self.buffers = {
            "5m": deque(maxlen=maxlen),
            "15m": deque(maxlen=maxlen),
            "1h": deque(maxlen=maxlen),
        }

        self.last_close_time = {
            "5m": None,
            "15m": None,
            "1h": None,
        }

        self.last_ws_close_time = {
            "5m": None,
            "15m": None,
            "1h": None,
        }

    # ==========================================
    # WS MESSAGE
    # ==========================================
    def on_ws_message(self, msg):
        # ✅ multiplex viene envuelto en {"stream": "...", "data": {...}}
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]

        if not isinstance(msg, dict):
            return

        # ✅ aceptar kline normal/multiplex y continuous_kline
        if msg.get("e") not in ("continuous_kline", "kline"):
            return

        k = msg["k"]
        tf = k["i"]

        self._last_price = float(k["c"])
        self._last_timestamp = int(k["t"])

        if tf not in self.buffers:
            return

        if not k["x"]:
            return

        open_time = int(k["t"])
        close_time = int(k["T"])

        if close_time == self.last_ws_close_time[tf]:
            return

        candle = {
            "timestamp": open_time,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "closed_at": datetime.utcfromtimestamp(close_time / 1000),
        }

        if self.buffers[tf] and self.buffers[tf][-1]["timestamp"] == open_time:
            self.buffers[tf][-1] = candle
        else:
            self.buffers[tf].append(candle)

        self.last_close_time[tf] = open_time
        self.last_ws_close_time[tf] = close_time
        self.closed_tfs.add(tf)

        print(f"\033[94m[DATA LAYER]\033[0m🕯️ STORED [{tf}] {candle['close']}")

    # ==========================================
    # EVENT CONSUMER
    # ==========================================
    def consume_closed_tf(self, tf: str) -> bool:
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

    def on_replay_candle(self, candle: dict, tf: str):

        self._last_price = float(candle["close"])
        self._last_timestamp = int(candle["timestamp"])

        if tf not in self.buffers:
            return

        close_time = candle["timestamp"]

        if close_time == self.last_close_time[tf]:
            return

        formatted = {
            "timestamp": close_time,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0)),
            "closed_at": datetime.utcfromtimestamp(close_time / 1000),
        }

        self.buffers[tf].append(formatted)
        self.last_close_time[tf] = close_time
        self.closed_tfs.add(tf)

        print(f"\033[95m[REPLAY DATA]\033[0m 🕯️ STORED [{tf}] {formatted['close']}")

    # ==========================================
    # HISTORICAL LOAD
    # ==========================================
    def load_historical(self, tf, df):
        if tf not in self.buffers:
            return

        for _, row in df.iterrows():
            ts = row["timestamp"]

            if hasattr(ts, "timestamp"):
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