from binance import ThreadedWebsocketManager
from live.config.settings import SYMBOL, TIMEFRAMES


class WSClient:
    def __init__(self, on_message):
        self.on_message = on_message
        self.twm = ThreadedWebsocketManager()

    def start(self):
        self.twm.start()

        for tf in TIMEFRAMES:
            self.twm.start_kline_futures_socket(
                symbol=SYMBOL.lower(),
                interval=tf,
                callback=self.on_message
            )

        print("📡 Futures WS connected:", SYMBOL, TIMEFRAMES)

    def stop(self):
        self.twm.stop()
