from collections import defaultdict, deque
from datetime import datetime
import threading


class DataBuffer:
    def __init__(self, timeframes, symbols=None, maxlen=400):
        self.timeframes = set(timeframes)
        self.symbols = set(symbols or [])

        self.closed_events = set()
        self.closed_events_lock = threading.Lock()

        self._last_price = {}
        self._last_timestamp = {}

        self.buffers = defaultdict(
            lambda: {
                tf: deque(maxlen=maxlen)
                for tf in self.timeframes
            }
        )

        self.last_close_time = defaultdict(
            lambda: {
                tf: None
                for tf in self.timeframes
            }
        )

        self.last_ws_close_time = defaultdict(
            lambda: {
                tf: None
                for tf in self.timeframes
            }
        )

    def _normalize_symbol(self, symbol):
        if not symbol:
            return None
        return symbol.upper()

    def _extract_symbol(self, msg):
        return self._normalize_symbol(
            msg.get("s") or msg.get("ps")
        )

    # ==========================================
    # WS MESSAGE
    # ==========================================
    def on_ws_message(self, msg):
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]

        if not isinstance(msg, dict):
            return

        if msg.get("e") not in ("continuous_kline", "kline"):
            return

        symbol = self._extract_symbol(msg)

        if not symbol:
            return

        if self.symbols and symbol not in self.symbols:
            return

        k = msg["k"]
        tf = k["i"]

        if tf not in self.timeframes:
            return

        self._last_price[symbol] = float(k["c"])
        self._last_timestamp[symbol] = int(k["t"])

        if not k["x"]:
            return

        open_time = int(k["t"])
        close_time = int(k["T"])

        if close_time == self.last_ws_close_time[symbol][tf]:
            return

        candle = {
            "symbol": symbol,
            "timestamp": open_time,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
            "quoteVolume": float(k.get("q", 0)),
            "closed_at": datetime.utcfromtimestamp(close_time / 1000),
        }

        if (
            self.buffers[symbol][tf]
            and self.buffers[symbol][tf][-1]["timestamp"] == open_time
        ):
            self.buffers[symbol][tf][-1] = candle
        else:
            self.buffers[symbol][tf].append(candle)

        self.last_close_time[symbol][tf] = open_time
        self.last_ws_close_time[symbol][tf] = close_time
        with self.closed_events_lock:
            self.closed_events.add((symbol, tf))

        #print(
        #    f"\033[94m[DATA LAYER]\033[0m "
        #    f"🕯️ STORED [{symbol}][{tf}] {candle['close']}"
        #)

    # ==========================================
    # EVENT CONSUMER
    # ==========================================
    def consume_closed_tf(self, symbol: str, tf: str) -> bool:
        symbol = self._normalize_symbol(symbol)
        event = (symbol, tf)

        with self.closed_events_lock:
            if event in self.closed_events:
                self.closed_events.remove(event)
                return True

        return False


    def consume_any_closed_tf(self, tf: str):
        with self.closed_events_lock:
            for symbol, closed_tf in tuple(self.closed_events):
                if closed_tf == tf:
                    self.closed_events.remove((symbol, closed_tf))
                    return symbol

        return None


    def consume_closed_event(self):
        with self.closed_events_lock:
            if not self.closed_events:
                return None

            event = next(iter(self.closed_events))
            self.closed_events.remove(event)

            return event
        
    def closed_events_snapshot(self):
        with self.closed_events_lock:
            return tuple(self.closed_events)

    # ==========================================
    # GETTERS
    # ==========================================
    def last_price(self, symbol: str):
        symbol = self._normalize_symbol(symbol)
        return self._last_price.get(symbol)

    def last_timestamp(self, symbol: str):
        symbol = self._normalize_symbol(symbol)
        return self._last_timestamp.get(symbol)

    def last_closed_candle(self, symbol: str, tf: str):
        symbol = self._normalize_symbol(symbol)

        if not self.buffers[symbol][tf]:
            return None

        return self.buffers[symbol][tf][-1]

    def get_candles(self, symbol: str, tf: str):
        symbol = self._normalize_symbol(symbol)
        return list(self.buffers[symbol].get(tf, []))

    # ==========================================
    # REPLAY
    # ==========================================
    def on_replay_candle(self, candle: dict, tf: str, symbol: str):
        symbol = self._normalize_symbol(symbol)

        if tf not in self.timeframes:
            return

        self._last_price[symbol] = float(candle["close"])
        self._last_timestamp[symbol] = int(candle["timestamp"])

        close_time = int(candle["timestamp"])

        if close_time == self.last_close_time[symbol][tf]:
            return

        formatted = {
            "symbol": symbol,
            "timestamp": close_time,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0)),
            "quoteVolume": float(
                candle.get("quoteVolume")
                or candle.get("quote_volume")
                or candle.get("quote_asset_volume")
                or candle.get("volume", 0) * candle.get("close", 0)
            ),
            "closed_at": datetime.utcfromtimestamp(close_time / 1000),
        }

        self.buffers[symbol][tf].append(formatted)
        self.last_close_time[symbol][tf] = close_time
        with self.closed_events_lock:
            self.closed_events.add((symbol, tf))

        print(
            f"\033[95m[REPLAY DATA]\033[0m "
            f"🕯️ STORED [{symbol}][{tf}] {formatted['close']}"
        )

    # ==========================================
    # HISTORICAL LOAD
    # ==========================================
    def load_historical(self, symbol: str, tf: str, df):
        symbol = self._normalize_symbol(symbol)

        if tf not in self.timeframes:
            return

        for _, row in df.iterrows():
            ts = row["timestamp"]

            if hasattr(ts, "timestamp"):
                close_time = int(ts.timestamp() * 1000)
            else:
                close_time = int(ts)

            if close_time == self.last_close_time[symbol][tf]:
                continue

            candle = {
                "symbol": symbol,
                "timestamp": close_time,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "quoteVolume": float(
                    row.get("quoteVolume")
                    if "quoteVolume" in row
                    else row.get("quote_volume")
                    if "quote_volume" in row
                    else row.get("quote_asset_volume")
                    if "quote_asset_volume" in row
                    else row["volume"] * row["close"]
                ),
                "closed_at": datetime.utcfromtimestamp(close_time / 1000),
            }

            self.buffers[symbol][tf].append(candle)
            self.last_close_time[symbol][tf] = close_time

        #print(
        #    f"\033[94m[DATA LAYER]\033[0m "
        #    f"📦 Loaded {len(df)} historical candles for [{symbol}][{tf}]"
        #)