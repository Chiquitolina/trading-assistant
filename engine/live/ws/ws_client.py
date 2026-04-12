import time
from binance import ThreadedWebsocketManager
from engine.live.config.settings import SYMBOL, TIMEFRAMES


class WSClient:
    def __init__(self, on_message, stale_after=20):
        self.on_message = on_message
        self.stale_after = stale_after

        self.twm = None
        self.running = False
        self.retries = 0

        self.is_connected = False
        self.last_message_at = 0.0
        self._is_reconnecting = False

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

                # si estamos reconectando, ejecutar reconexión
                if self._is_reconnecting:
                    self._reconnect()
                    continue

                # heartbeat: si no llegan mensajes por mucho tiempo, reconectar
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

        # limpiar SIEMPRE cualquier manager previo
        self._stop_ws()

        try:
            twm = ThreadedWebsocketManager()
            twm.start()

            for tf in TIMEFRAMES:
                twm.start_kline_futures_socket(
                    symbol=SYMBOL.lower(),
                    interval=tf,
                    callback=self._handle_message
                )

            # recién si todo salió bien, asignamos
            self.twm = twm

            self.retries = 0
            self._is_reconnecting = False

            # socket inicializado, pero todavía no confirmado por mensajes reales
            self.is_connected = False
            self.last_message_at = 0.0

            print(
                f"\n\033[94m[WS CLIENT]\033[0m 📡 Futures WS initialized: "
                f"{SYMBOL} {TIMEFRAMES}\n"
            )

        except Exception:
            # importantísimo: limpiar cualquier estado parcial
            self._stop_ws()
            self.is_connected = False
            self.last_message_at = 0.0
            raise

    def _handle_message(self, msg):
        try:
            # error del socket reportado por la librería
            if isinstance(msg, dict) and msg.get("e") == "error":
                print(f"\033[94m[WS CLIENT]\033[0m ⚠️ WS error: {msg}")
                self.is_connected = False
                self._is_reconnecting = True
                return

            # heartbeat real
            self.last_message_at = time.time()
            self.is_connected = True

            # pasar mensaje al motor
            self.on_message(msg)

        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ❌ Error inside WS callback: {e}")

    def _reconnect(self):
        if not self.running:
            return

        self.is_connected = False
        self.last_message_at = 0.0

        # evitar reentradas raras
        self._stop_ws()

        self.retries += 1
        delay = min(2 ** min(self.retries, 5), 30)

        print(f"\033[94m[WS CLIENT]\033[0m 🔄 Reconnecting WS in {delay}s...")
        time.sleep(delay)

        if not self.running:
            return

        try:
            self._connect()
        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ❌ Reconnect failed: {e}")
            self._is_reconnecting = True
            self.is_connected = False
            self.last_message_at = 0.0

    def _stop_ws(self):
        twm = self.twm
        self.twm = None

        if not twm:
            return

        try:
            twm.stop()
            time.sleep(1)
        except Exception as e:
            print(f"\033[94m[WS CLIENT]\033[0m ⚠️ Error stopping WS: {e}")