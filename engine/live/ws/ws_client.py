import time
from binance import ThreadedWebsocketManager
from engine.live.config.settings import SYMBOL
import threading

class WSClient:
    def __init__(self, on_message, timeframes, stale_after=20):
        self.on_message = on_message
        self.timeframes = timeframes
        self.stale_after = stale_after

        self.twm = None
        self.running = False
        self.retries = 0

        self.is_connected = False
        self.last_message_at = 0.0
        self._is_reconnecting = False
        
        self._reconnect_lock = threading.Lock()

    # =========================
    # PUBLIC
    # =========================
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

    # =========================
    # INTERNAL
    # =========================
    def _connect(self):
        print("\n\033[94m[WS CLIENT]\033[0m 🔌 Connecting WS...")

        self._stop_ws()

        try:
            twm = ThreadedWebsocketManager()
            twm.start()

            streams = [
                f"{SYMBOL.lower()}@kline_{tf}"
                for tf in self.timeframes
            ]

            twm.start_multiplex_socket(
                streams=streams,
                callback=self._handle_message
            )

            self.twm = twm

            self.retries = 0
            self._is_reconnecting = False

            self.is_connected = False
            self.last_message_at = 0.0

            print(
                f"\n\033[94m[WS CLIENT]\033[0m 📡 Futures WS multiplex initialized: "
                f"{streams}\n"
            )

        except Exception:
            self._stop_ws()
            self.is_connected = False
            self.last_message_at = 0.0
            raise

    def _handle_message(self, msg):
        try:
            if isinstance(msg, dict) and msg.get("e") == "error":
                print(f"\033[94m[WS CLIENT]\033[0m ⚠️ WS error: {msg}")
                self.is_connected = False
                self._is_reconnecting = True
                return

            self.last_message_at = time.time()
            self.is_connected = True

            self.on_message(msg)

        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ❌ Error inside WS callback: {e}")

    def _reconnect(self):

        with self._reconnect_lock:

            if not self.running:
                return

            if not self._is_reconnecting:
                return

            print("\033[94m[WS CLIENT]\033[0m 🔄 Starting reconnect...")

            self.is_connected = False
            self.last_message_at = 0.0

            self._stop_ws()

            self.retries += 1

            delay = min(2 ** min(self.retries, 5), 30)

            print(
                f"\033[94m[WS CLIENT]\033[0m "
                f"🔄 Reconnecting WS in {delay}s..."
            )

            time.sleep(delay)

            if not self.running:
                return

            try:

                self._connect()

            except Exception as e:

                print(
                    f"\033[94m[WS CLIENT]\033[0m "
                    f"❌ Reconnect failed: {e}"
                )

                self._is_reconnecting = True
                self.is_connected = False
                self.last_message_at = 0.0
                
    def _stop_ws(self):

        twm = self.twm

        self.twm = None

        if not twm:
            return

        try:

            print(
                "\033[94m[WS CLIENT]\033[0m "
                "🛑 Stopping WS manager..."
            )

            twm.stop()

            # 🔥 IMPORTANTE
            # darle tiempo al loop/thread a morir
            time.sleep(5)

            print(
                "\033[94m[WS CLIENT]\033[0m "
                "✅ WS manager stopped"
            )

        except Exception as e:

            print(
                f"\033[94m[WS CLIENT]\033[0m "
                f"⚠️ Error stopping WS: {e}"
            )