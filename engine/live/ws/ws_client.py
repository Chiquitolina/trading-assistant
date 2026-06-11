import time
import threading
from binance import ThreadedWebsocketManager


class WSClient:
    def __init__(self, on_message, timeframes, symbols, stale_after=20, chunk_size=40):
        self.on_message = on_message
        self.timeframes = timeframes
        self.symbols = symbols
        self.stale_after = stale_after
        self.chunk_size = chunk_size

        self.twm = None
        self.running = False
        self.retries = 0

        self.is_connected = False
        self.last_message_at = 0.0
        self._is_reconnecting = False

        self._reconnect_lock = threading.Lock()

        self._last_reconnect = 0
        self.min_reconnect_interval = 10

        self.handshake_failures = 0
        
    def _chunk_list(self, items, size):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def start(self):
        if self.running:
            return

        self.running = True

        try:
            self._connect()
        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ❌ Initial connect failed: {e}")
            self.is_connected = False
            self._is_reconnecting = True

    def run(self):
        while self.running:
            try:
                now = time.time()

                if self._is_reconnecting:
                    self._reconnect()
                    continue

                if self.is_connected and self.last_message_at > 0:
                    if now - self.last_message_at > self.stale_after:
                        print(
                            f"\033[94m[WS CLIENT]\033[0m ⚠️ WS stale detected: "
                            f"no messages for {now - self.last_message_at:.1f}s"
                        )
                        self.is_connected = False
                        self._is_reconnecting = True

                time.sleep(1)

            except Exception as e:
                print(f"\033[94m[WS CLIENT]\033[0m ❌ WS loop error: {e}")
                self.is_connected = False
                self._is_reconnecting = True
                time.sleep(3)

    def stop(self):
        self.running = False
        self._is_reconnecting = False
        self.is_connected = False
        self.last_message_at = 0.0
        self._stop_ws()

    def _connect(self):
        print("\n\033[94m[WS CLIENT]\033[0m 🔌 Connecting WS...")

        self._stop_ws()

        try:
            self.twm = ThreadedWebsocketManager()
            self.twm.start()

            time.sleep(3)

            total_streams = 0

            for i, symbols_chunk in enumerate(
                self._chunk_list(self.symbols, self.chunk_size),
                start=1
            ):
                streams = [
                    f"{symbol.lower()}@kline_{tf}"
                    for symbol in symbols_chunk
                    for tf in self.timeframes
                ]

                self.twm.start_multiplex_socket(
                    streams=streams,
                    callback=self._handle_message
                )

                total_streams += len(streams)

                print(
                    f"\033[94m[WS CLIENT]\033[0m "
                    f"📡 WS group {i}: symbols={len(symbols_chunk)} streams={len(streams)}"
                )

                time.sleep(1)

            self.retries = 0
            self._is_reconnecting = False
            self.handshake_failures = 0
            self.is_connected = False
            self.last_message_at = 0.0

            print(
                f"\n\033[94m[WS CLIENT]\033[0m "
                f"📡 WS initialized total_streams={total_streams}\n"
            )

        except Exception:
            self._stop_ws()
            self.is_connected = False
            self.last_message_at = 0.0
            raise

    def _handle_message(self, msg):
        try:
            if isinstance(msg, dict) and msg.get("e") == "error":
                print(
                    f"\033[94m[WS CLIENT]\033[0m "
                    f"⚠️ WS error: {msg}"
                )

                self.is_connected = False
                self._is_reconnecting = True
                return

            self.last_message_at = time.time()
            self.is_connected = True

            self.on_message(msg)

        except Exception as e:
            print(
                f"\033[94m[WS CLIENT]\033[0m "
                f"❌ Callback error: {e}"
            )

    def _reconnect(self):
        with self._reconnect_lock:
            if not self.running:
                return

            if not self._is_reconnecting:
                return

            now = time.time()

            if now - self._last_reconnect < self.min_reconnect_interval:
                print("\033[94m[WS CLIENT]\033[0m ⏳ Reconnect cooldown active")
                return

            self._last_reconnect = now

            print("\033[94m[WS CLIENT]\033[0m 🔄 Starting reconnect...")

            self.is_connected = False
            self.last_message_at = 0.0

            self._stop_ws()

            self.retries += 1

            delay = min(2 ** min(self.retries, 6), 60)
            delay = max(delay, 8)

            print(
                f"\033[94m[WS CLIENT]\033[0m 🔄 Reconnecting in {delay}s..."
            )

            time.sleep(delay)

            if not self.running:
                return

            try:
                self._connect()

            except Exception as e:
                print(f"\033[94m[WS CLIENT]\033[0m ❌ Reconnect failed: {e}")

                self.handshake_failures += 1

                if self.handshake_failures >= 5:
                    print("\033[94m[WS CLIENT]\033[0m ❌ Too many failures, backing off hard")
                    time.sleep(30)

                self._is_reconnecting = True
                self.is_connected = False
                self.last_message_at = 0.0

    def _stop_ws(self):
        twm = self.twm
        self.twm = None

        if not twm:
            return

        try:
            print("\033[94m[WS CLIENT]\033[0m 🛑 Stopping WS manager...")

            twm.stop()
            time.sleep(5)

            print("\033[94m[WS CLIENT]\033[0m ✅ WS manager stopped")

        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ⚠️ Stop error: {e}")