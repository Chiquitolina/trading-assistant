from statistics import median


class VolumeExhaustionCollector:
    """
    Research-only collector.

    Detecta eventos anormales de volumen en velas cerradas de 1m.
    No genera señales, no abre posiciones y no ejecuta órdenes.
    """

    def __init__(
        self,
        buffer,
        timeframe="1m",
        baseline_lookback=30,
        min_relative_volume=2.0,
    ):
        self.buffer = buffer
        self.timeframe = timeframe
        self.baseline_lookback = baseline_lookback
        self.min_relative_volume = min_relative_volume

        self.events_detected = 0

        # Último boundary procesado por símbolo.
        # Evita procesar dos o más veces la misma vela.
        self._last_processed_close_time = {}

    def evaluate(
        self,
        symbol: str,
        close_time: int,
    ):
        symbol = str(symbol).upper()
        close_time = int(close_time)

        # ==========================================
        # IDEMPOTENCIA
        # ==========================================
        last_processed = (
            self._last_processed_close_time.get(symbol)
        )

        if (
            last_processed is not None
            and close_time <= last_processed
        ):
            return None

        # ==========================================
        # VELA EXACTA DEL EVENTO
        # ==========================================
        current = self.buffer.closed_candle_at(
            symbol,
            self.timeframe,
            close_time,
        )

        if current is None:
            return None

        # Marcamos este boundary como procesado
        # solamente después de comprobar que existe.
        self._last_processed_close_time[symbol] = (
            close_time
        )

        # ==========================================
        # HISTÓRICO
        # ==========================================
        candles = self.buffer.get_candles(
            symbol,
            self.timeframe,
        )

        if len(candles) < self.baseline_lookback + 1:
            return None

        current_timestamp = int(
            current["timestamp"]
        )

        # Tomamos exclusivamente velas anteriores
        # a la vela que estamos evaluando.
        previous_candles = [
            candle
            for candle in candles
            if int(candle["timestamp"])
            < current_timestamp
        ]

        if len(previous_candles) < self.baseline_lookback:
            return None

        baseline = previous_candles[
            -self.baseline_lookback:
        ]

        # ==========================================
        # BASELINE DE VOLUMEN
        # ==========================================
        volumes = [
            float(candle["volume"])
            for candle in baseline
            if float(candle["volume"]) > 0
        ]

        if not volumes:
            return None

        baseline_volume = median(volumes)

        if baseline_volume <= 0:
            return None

        current_volume = float(
            current["volume"]
        )

        relative_volume = (
            current_volume / baseline_volume
        )

        if relative_volume < self.min_relative_volume:
            return None

        # ==========================================
        # PRECIO
        # ==========================================
        open_price = float(current["open"])
        close_price = float(current["close"])
        high_price = float(current["high"])
        low_price = float(current["low"])

        if open_price <= 0:
            return None

        return_pct = (
            (close_price / open_price) - 1
        ) * 100

        range_pct = (
            (high_price - low_price)
            / open_price
        ) * 100

        if close_price > open_price:
            candle_direction = "BUY"

        elif close_price < open_price:
            candle_direction = "SELL"

        else:
            candle_direction = "NEUTRAL"

        # ==========================================
        # EVENT
        # ==========================================
        event = {
            "symbol": symbol,
            "timeframe": self.timeframe,

            # Boundary que originó la evaluación.
            "close_time": close_time,

            # Open timestamp de la vela.
            "timestamp": current_timestamp,

            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,

            "volume": current_volume,
            "baseline_volume": baseline_volume,
            "relative_volume": relative_volume,

            "return_pct": return_pct,
            "range_pct": range_pct,
            "candle_direction": candle_direction,
        }

        self.events_detected += 1

        return event