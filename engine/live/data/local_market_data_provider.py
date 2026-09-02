import threading

from data.market_data import fetch_history
from engine.live.ws.ws_client import WSClient


class LocalMarketDataProvider:
    def __init__(
        self,
        buffer,
        symbols,
        timeframes,
        days_by_tf,
        chunk_size=15,
        stale_after=90,
    ):
        self.buffer = buffer
        self.symbols = list(symbols)
        self.timeframes = list(timeframes)
        self.days_by_tf = dict(days_by_tf)
        self.chunk_size = chunk_size
        self.stale_after = stale_after

        self.ws = None
        self.ws_thread = None

    @property
    def is_connected(self):
        return bool(
            self.ws
            and self.ws.is_connected
        )

    def load_history(self):
        for symbol in self.symbols:
            print(symbol)

            for timeframe in self.timeframes:
                days = self.days_by_tf.get(
                    timeframe,
                    3,
                )

                df_history = fetch_history(
                    symbol,
                    timeframe,
                    days,
                )

                self.buffer.load_historical(
                    symbol,
                    timeframe,
                    df_history,
                )

    def start(self):
        if self.ws is not None:
            return

        self.ws = WSClient(
            self.buffer.on_ws_message,
            timeframes=self.timeframes,
            symbols=self.symbols,
            chunk_size=self.chunk_size,
            stale_after=self.stale_after,
        )

        self.ws.start()

        self.ws_thread = threading.Thread(
            target=self.ws.run,
            daemon=True,
            name="local-market-data-ws",
        )

        self.ws_thread.start()

    def stop(self):
        if self.ws is None:
            return

        self.ws.stop()
        self.ws = None
        self.ws_thread = None