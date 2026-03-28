import time
from binance import ThreadedWebsocketManager
from engine.live.config.settings import SYMBOL, TIMEFRAMES


class WSClient:

    def __init__(self, on_message):
        self.on_message = on_message
        self.twm = None
        self.running = False
        self.retries = 0

        # 🔥 control interno
        self._is_reconnecting = False

    # =========================
    # PUBLIC
    # =========================
    def start(self):
        self.running = True
        self._connect()

    def run(self):
        """
        Loop externo que mantiene vivo el WS
        """
        while True:
            try:
                if not self.running and self._is_reconnecting:
                    self._reconnect()

                time.sleep(1)

            except Exception as e:
                print(f"❌ WS loop error: {e}")
                time.sleep(5)

    def stop(self):
        self.running = False
        self._is_reconnecting = False
        self._stop_ws()

    # =========================
    # INTERNAL
    # =========================
    def _connect(self):
        print("\n\033[94m[WS CLIENT]\033[0m 🔌 Connecting WS...\n")

        self.twm = ThreadedWebsocketManager()
        self.twm.start()

        for tf in TIMEFRAMES:
            self.twm.start_kline_futures_socket(
                symbol=SYMBOL.lower(),
                interval=tf,
                callback=self._handle_message
            )

        self.retries = 0
        self.running = True
        self._is_reconnecting = False  # 🔥 reset flag

        print(f"\n\033[94m[WS CLIENT]\033[0m 📡 Futures WS connected: {SYMBOL} {TIMEFRAMES}\n")

    def _handle_message(self, msg):
        """
        ⚠️ NO reiniciar desde acá
        """

        # 🔥 detectar error WS (ej: ReadLoopClosed)
        if msg.get("e") == "error" or "error" in msg:

            # 👇 evitar spam infinito
            if self._is_reconnecting:
                return

            print(f"⚠️ WS error: {msg}")

            self.running = False
            self._is_reconnecting = True  # 🔥 bloquear nuevos errores

            return

        # mensaje válido
        self.on_message(msg)

    def _reconnect(self):
        """
        🔁 Reconexión controlada
        """

        # 👇 evitar reconexiones múltiples simultáneas
        if not self._is_reconnecting:
            return

        self._stop_ws()

        # 🔁 backoff exponencial
        self.retries += 1
        delay = min(2 ** self.retries, 30)

        print(f"🔄 Reconnecting WS in {delay}s...")
        time.sleep(delay)

        self._connect()

    def _stop_ws(self):
        """
        🧹 cerrar WS correctamente (threads)
        """
        try:
            if self.twm:
                self.twm.stop()
                time.sleep(1)  # 🔥 dar tiempo a cerrar threads
        except Exception as e:
            print(f"⚠️ Error stopping WS: {e}")

        self.twm = None