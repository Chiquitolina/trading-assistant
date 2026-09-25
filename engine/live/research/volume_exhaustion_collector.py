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

    def evaluate(self, symbol: str):
        candles = self.buffer.get_candles(
            symbol,
            self.timeframe,
        )

        # Necesitamos:
        # baseline_lookback velas anteriores
        # + la vela actual.
        if len(candles) < self.baseline_lookback + 1:
            return None

        current = candles[-1]

        # IMPORTANTE:
        # excluimos la vela actual del baseline.
        baseline = candles[
            -(self.baseline_lookback + 1):-1
        ]

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

        event = {
            "symbol": symbol,
            "timeframe": self.timeframe,
            "timestamp": current["timestamp"],

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