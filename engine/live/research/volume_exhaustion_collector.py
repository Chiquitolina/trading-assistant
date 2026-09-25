from statistics import median


class VolumeExhaustionCollector:
    """
    Research-only collector for 1m volume exhaustion research.

    Detecta anomalías de volumen y captura contexto multivela.
    No genera señales ni ejecuta órdenes.
    """

    def __init__(
        self,
        buffer,
        timeframe="1m",
        baseline_lookback=30,
        min_relative_volume=2.0,
        rsi_period=14,
    ):
        self.buffer = buffer
        self.timeframe = timeframe
        self.baseline_lookback = baseline_lookback
        self.min_relative_volume = min_relative_volume
        self.rsi_period = rsi_period

        self.events_detected = 0
        self._last_processed_close_time = {}

    # ==========================================
    # HELPERS
    # ==========================================

    @staticmethod
    def _pct_change(start, end):
        start = float(start)
        end = float(end)

        if start <= 0:
            return None

        return ((end / start) - 1) * 100

    @staticmethod
    def _calculate_rsi(candles, period=14):
        if len(candles) < period + 1:
            return None

        closes = [
            float(candle["close"])
            for candle in candles[-(period + 1):]
        ]

        gains = []
        losses = []

        for previous, current in zip(
            closes[:-1],
            closes[1:],
        ):
            change = current - previous

            if change > 0:
                gains.append(change)
                losses.append(0.0)
            elif change < 0:
                gains.append(0.0)
                losses.append(abs(change))
            else:
                gains.append(0.0)
                losses.append(0.0)

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            if avg_gain == 0:
                return 50.0
            return 100.0

        rs = avg_gain / avg_loss

        return 100 - (100 / (1 + rs))

    def _window_volume_ratio(
        self,
        window,
        baseline_volume,
    ):
        if not window or baseline_volume <= 0:
            return None

        actual = sum(
            float(candle["volume"])
            for candle in window
        )

        expected = (
            baseline_volume * len(window)
        )

        if expected <= 0:
            return None

        return actual / expected

    # ==========================================
    # EVALUATE
    # ==========================================

    def evaluate(
        self,
        symbol: str,
        close_time: int,
    ):
        symbol = str(symbol).upper()
        close_time = int(close_time)

        # ======================================
        # IDEMPOTENCIA
        # ======================================

        last_processed = (
            self._last_processed_close_time.get(symbol)
        )

        if (
            last_processed is not None
            and close_time <= last_processed
        ):
            return None

        # ======================================
        # VELA EXACTA
        # ======================================

        current = self.buffer.closed_candle_at(
            symbol,
            self.timeframe,
            close_time,
        )

        if current is None:
            return None

        self._last_processed_close_time[symbol] = (
            close_time
        )

        current_timestamp = int(
            current["timestamp"]
        )

        candles = self.buffer.get_candles(
            symbol,
            self.timeframe,
        )

        previous_candles = [
            candle
            for candle in candles
            if int(candle["timestamp"])
            < current_timestamp
        ]

        if len(previous_candles) < max(
            self.baseline_lookback,
            self.rsi_period + 1,
        ):
            return None

        # ======================================
        # VOLUME BASELINE
        # ======================================

        baseline = previous_candles[
            -self.baseline_lookback:
        ]

        baseline_volumes = [
            float(candle["volume"])
            for candle in baseline
            if float(candle["volume"]) > 0
        ]

        if not baseline_volumes:
            return None

        baseline_volume = median(
            baseline_volumes
        )

        if baseline_volume <= 0:
            return None

        current_volume = float(
            current["volume"]
        )

        relative_volume = (
            current_volume / baseline_volume
        )

        # Seguimos usando esto SOLAMENTE
        # como puerta amplia del dataset.
        if relative_volume < self.min_relative_volume:
            return None

        # ======================================
        # SECUENCIA HASTA CURRENT
        # ======================================

        sequence = previous_candles + [current]

        # ======================================
        # VOLUME WINDOWS
        # ======================================

        volume_2m_ratio = self._window_volume_ratio(
            sequence[-2:],
            baseline_volume,
        )

        volume_3m_ratio = self._window_volume_ratio(
            sequence[-3:],
            baseline_volume,
        )

        volume_4m_ratio = self._window_volume_ratio(
            sequence[-4:],
            baseline_volume,
        )

        volume_5m_ratio = self._window_volume_ratio(
            sequence[-5:],
            baseline_volume,
        )

        high_volume_candles_5m = sum(
            1
            for candle in sequence[-5:]
            if (
                float(candle["volume"])
                / baseline_volume
            ) >= self.min_relative_volume
        )

        # ======================================
        # PRICE MOVEMENT
        # ======================================

        def move_for_window(minutes):
            if len(sequence) < minutes:
                return None

            window = sequence[-minutes:]

            return self._pct_change(
                window[0]["open"],
                window[-1]["close"],
            )

        move_2m_pct = move_for_window(2)
        move_3m_pct = move_for_window(3)
        move_5m_pct = move_for_window(5)
        move_10m_pct = move_for_window(10)

        # ======================================
        # CURRENT CANDLE GEOMETRY
        # ======================================

        open_price = float(current["open"])
        high_price = float(current["high"])
        low_price = float(current["low"])
        close_price = float(current["close"])

        if open_price <= 0:
            return None

        return_pct = self._pct_change(
            open_price,
            close_price,
        )

        range_price = high_price - low_price

        range_pct = (
            (range_price / open_price) * 100
            if range_price >= 0
            else None
        )

        body_price = abs(
            close_price - open_price
        )

        body_pct = (
            body_price / range_price
            if range_price > 0
            else 0.0
        )

        upper_wick = (
            high_price
            - max(open_price, close_price)
        )

        lower_wick = (
            min(open_price, close_price)
            - low_price
        )

        upper_wick_pct = (
            upper_wick / range_price
            if range_price > 0
            else 0.0
        )

        lower_wick_pct = (
            lower_wick / range_price
            if range_price > 0
            else 0.0
        )

        close_location = (
            (close_price - low_price)
            / range_price
            if range_price > 0
            else 0.5
        )

        if close_price > open_price:
            candle_direction = "BUY"
        elif close_price < open_price:
            candle_direction = "SELL"
        else:
            candle_direction = "NEUTRAL"

        # ======================================
        # RSI
        # ======================================

        rsi_1m = self._calculate_rsi(
            sequence,
            period=self.rsi_period,
        )

        # ======================================
        # PRICE / VOLUME EFFICIENCY
        # ======================================

        price_volume_efficiency = (
            abs(return_pct) / relative_volume
            if relative_volume > 0
            else None
        )

        efficiency_3m = (
            abs(move_3m_pct) / volume_3m_ratio
            if (
                move_3m_pct is not None
                and volume_3m_ratio
                and volume_3m_ratio > 0
            )
            else None
        )

        efficiency_5m = (
            abs(move_5m_pct) / volume_5m_ratio
            if (
                move_5m_pct is not None
                and volume_5m_ratio
                and volume_5m_ratio > 0
            )
            else None
        )

        # ======================================
        # EVENT
        # ======================================

        event = {
            "symbol": symbol,
            "timeframe": self.timeframe,
            "close_time": close_time,
            "timestamp": current_timestamp,

            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,

            # Volume
            "volume": current_volume,
            "baseline_volume": baseline_volume,
            "relative_volume": relative_volume,
            "volume_2m_ratio": volume_2m_ratio,
            "volume_3m_ratio": volume_3m_ratio,
            "volume_4m_ratio": volume_4m_ratio,
            "volume_5m_ratio": volume_5m_ratio,
            "high_volume_candles_5m": (
                high_volume_candles_5m
            ),

            # Price movement
            "return_pct": return_pct,
            "move_2m_pct": move_2m_pct,
            "move_3m_pct": move_3m_pct,
            "move_5m_pct": move_5m_pct,
            "move_10m_pct": move_10m_pct,

            # Candle geometry
            "range_pct": range_pct,
            "body_pct": body_pct,
            "upper_wick_pct": upper_wick_pct,
            "lower_wick_pct": lower_wick_pct,
            "close_location": close_location,
            "candle_direction": candle_direction,

            # Momentum
            "rsi_1m": rsi_1m,

            # Efficiency
            "price_volume_efficiency": (
                price_volume_efficiency
            ),
            "efficiency_3m": efficiency_3m,
            "efficiency_5m": efficiency_5m,
        }

        self.events_detected += 1

        return event