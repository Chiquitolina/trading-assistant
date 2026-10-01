import csv
import os

from datetime import datetime, timezone
from statistics import median

import pandas as pd

from signals.indicators.atr import add_atr

from signals.signals_engine import (
    SWING_LOOKBACK,
    detect_swing_low,
    detect_swing_high,
    find_last_swing_low,
    find_last_swing_high,
    swing_distance_low_pct,
    swing_distance_high_pct,
)


class VolumeExhaustionCollector:
    """
    Research-only collector for 1m volume exhaustion research.

    Detecta anomalías de volumen y captura contexto multivela.
    Persiste cada candidato en CSV para análisis posterior.

    También captura contexto de swings point-in-time para:
    1m, 5m, 15m, 30m, 1h y 4h.

    No genera señales ni ejecuta órdenes.
    """

    SWING_TIMEFRAMES = (
        "1m",
        "5m",
        "15m",
        "30m",
        "1h",
        "4h",
    )

    SWING_NEAR_MULT = {
        "1m": 1.2,
        "5m": 1.2,
        "15m": 1.2,
        "30m": 1.1,
        "1h": 1.0,
        "4h": 0.8,
    }

    def __init__(
        self,
        buffer,
        timeframe="1m",
        baseline_lookback=30,
        min_relative_volume=2.0,
        rsi_period=14,
        events_path="volume_exhaustion_events.csv",
    ):
        self.buffer = buffer
        self.timeframe = timeframe
        self.baseline_lookback = baseline_lookback
        self.min_relative_volume = min_relative_volume
        self.rsi_period = rsi_period
        self.events_path = events_path

        self.events_detected = 0

        # Último boundary procesado por símbolo.
        # Evita procesar dos veces la misma vela.
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
    def _timestamp_to_iso(timestamp_ms):
        """
        Convierte timestamp Unix en milisegundos
        a fecha ISO UTC legible.
        """
        if timestamp_ms is None:
            return None

        return datetime.fromtimestamp(
            int(timestamp_ms) / 1000,
            tz=timezone.utc,
        ).isoformat()

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
    # SWING HELPERS
    # ==========================================

    def _find_last_swing_with_timestamp(
        self,
        df,
        side,
    ):
        """
        Localiza exactamente la misma clase de swing
        utilizada por SignalEngine, pero además devuelve
        el timestamp de la vela que originó el swing.

        No modifica las funciones existentes del engine.
        """
        if df is None or df.empty:
            return None, None

        idx = len(df) - 1

        if side == "LOW":
            detector = detect_swing_low
            price_column = "low"

        elif side == "HIGH":
            detector = detect_swing_high
            price_column = "high"

        else:
            raise ValueError(
                f"Unsupported swing side: {side}"
            )

        for j in range(
            idx - 1,
            SWING_LOOKBACK,
            -1,
        ):
            if detector(
                df,
                j,
                SWING_LOOKBACK,
            ):
                price = float(
                    df.iloc[j][price_column]
                )

                timestamp = int(
                    df.iloc[j]["timestamp"]
                )

                return price, timestamp

        return None, None

    def _build_swing_context(
        self,
        symbol,
        timeframe,
        event_timestamp,
        event_price,
    ):
        """
        Construye contexto de swing usando únicamente
        velas disponibles hasta el instante del evento.

        Devuelve:
        - swing low/high
        - timestamps
        - timestamps UTC
        - distancias %
        - near swing
        - ATR utilizado
        """
        candles = self.buffer.get_candles(
            symbol,
            timeframe,
        )

        if not candles:
            return {}

        tf_ms = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }.get(timeframe)

        if tf_ms is None:
            return {}

        # ------------------------------------------
        # POINT-IN-TIME FILTER
        # ------------------------------------------
        #
        # Una vela HTF sólo puede utilizarse si ya
        # estaba completamente cerrada cuando ocurrió
        # el evento 1m.
        #
        # timestamp es el OPEN TIME.
        # Por eso:
        #
        # candle_open + tf_ms - 1 <= event_timestamp
        #
        available_candles = [
            candle
            for candle in candles
            if (
                int(candle["timestamp"])
                + tf_ms
                - 1
            ) <= int(event_timestamp)
        ]

        if len(
            available_candles
        ) < SWING_LOOKBACK + 5:
            return {}

        df = pd.DataFrame(
            available_candles
        )

        # ATR del mismo timeframe.
        atr = None

        if len(df) >= 14:
            df = add_atr(
                df,
                period=14,
            )

            if (
                "atr" in df.columns
                and not pd.isna(
                    df.iloc[-1]["atr"]
                )
            ):
                atr = float(
                    df.iloc[-1]["atr"]
                )

        # ------------------------------------------
        # SWINGS
        # ------------------------------------------

        swing_low = find_last_swing_low(
            df,
            len(df) - 1,
        )

        swing_high = find_last_swing_high(
            df,
            len(df) - 1,
        )

        # Misma detección, pero recuperando
        # timestamp para auditoría.
        (
            swing_low_check,
            swing_low_ts,
        ) = self._find_last_swing_with_timestamp(
            df,
            "LOW",
        )

        (
            swing_high_check,
            swing_high_ts,
        ) = self._find_last_swing_with_timestamp(
            df,
            "HIGH",
        )

        # Safety check:
        # ambas rutas deberían devolver
        # exactamente el mismo precio.
        if (
            swing_low is not None
            and swing_low_check is not None
            and float(swing_low)
            != float(swing_low_check)
        ):
            raise RuntimeError(
                "Swing LOW mismatch | "
                f"symbol={symbol} | "
                f"timeframe={timeframe} | "
                f"engine={swing_low} | "
                f"collector={swing_low_check}"
            )

        if (
            swing_high is not None
            and swing_high_check is not None
            and float(swing_high)
            != float(swing_high_check)
        ):
            raise RuntimeError(
                "Swing HIGH mismatch | "
                f"symbol={symbol} | "
                f"timeframe={timeframe} | "
                f"engine={swing_high} | "
                f"collector={swing_high_check}"
            )

        if swing_low is not None:
            swing_low = float(swing_low)

        if swing_high is not None:
            swing_high = float(swing_high)

        # ------------------------------------------
        # DISTANCES
        # ------------------------------------------

        dist_low = swing_distance_low_pct(
            event_price,
            swing_low,
        )

        dist_high = swing_distance_high_pct(
            event_price,
            swing_high,
        )

        # ------------------------------------------
        # NEAR
        # ------------------------------------------

        near_mult = (
            self.SWING_NEAR_MULT.get(
                timeframe,
                1.2,
            )
        )

        near_low = (
            swing_low is not None
            and atr is not None
            and abs(
                event_price - swing_low
            ) <= (
                atr * near_mult
            )
        )

        near_high = (
            swing_high is not None
            and atr is not None
            and abs(
                event_price - swing_high
            ) <= (
                atr * near_mult
            )
        )

        return {
            "swing_low": swing_low,
            "swing_low_ts": swing_low_ts,
            "swing_low_time_utc": (
                self._timestamp_to_iso(
                    swing_low_ts
                )
                if swing_low_ts is not None
                else None
            ),

            "swing_high": swing_high,
            "swing_high_ts": swing_high_ts,
            "swing_high_time_utc": (
                self._timestamp_to_iso(
                    swing_high_ts
                )
                if swing_high_ts is not None
                else None
            ),

            "dist_swing_low_pct": dist_low,
            "dist_swing_high_pct": dist_high,

            "near_swing_low": near_low,
            "near_swing_high": near_high,

            "swing_atr": atr,
            "swing_near_mult": near_mult,
        }

    def _build_all_swing_contexts(
        self,
        symbol,
        event_timestamp,
        event_price,
    ):
        """
        Captura swings para todos los timeframes
        relevantes del evento.
        """
        result = {}

        for timeframe in self.SWING_TIMEFRAMES:
            context = self._build_swing_context(
                symbol=symbol,
                timeframe=timeframe,
                event_timestamp=event_timestamp,
                event_price=event_price,
            )

            result[timeframe] = context

        return result

    def _save_event(self, event):
        """
        Persiste un candidato en CSV.

        El header se crea automáticamente
        la primera vez.
        """
        file_exists = os.path.exists(
            self.events_path
        )

        fieldnames = list(event.keys())

        with open(
            self.events_path,
            "a",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fieldnames,
            )

            if not file_exists:
                writer.writeheader()

            writer.writerow(event)

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
            self._last_processed_close_time.get(
                symbol
            )
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

        # Puerta amplia del dataset.
        # Todavía NO significa exhaustion confirmado.
        if relative_volume < self.min_relative_volume:
            return None

        # ======================================
        # SECUENCIA HASTA CURRENT
        # ======================================

        sequence = previous_candles + [
            current
        ]

        # ======================================
        # VOLUME WINDOWS
        # ======================================

        volume_2m_ratio = (
            self._window_volume_ratio(
                sequence[-2:],
                baseline_volume,
            )
        )

        volume_3m_ratio = (
            self._window_volume_ratio(
                sequence[-3:],
                baseline_volume,
            )
        )

        volume_4m_ratio = (
            self._window_volume_ratio(
                sequence[-4:],
                baseline_volume,
            )
        )

        volume_5m_ratio = (
            self._window_volume_ratio(
                sequence[-5:],
                baseline_volume,
            )
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

        open_price = float(
            current["open"]
        )

        high_price = float(
            current["high"]
        )

        low_price = float(
            current["low"]
        )

        close_price = float(
            current["close"]
        )

        if open_price <= 0:
            return None

        return_pct = self._pct_change(
            open_price,
            close_price,
        )

        range_price = (
            high_price - low_price
        )

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
            - max(
                open_price,
                close_price,
            )
        )

        lower_wick = (
            min(
                open_price,
                close_price,
            )
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
            abs(return_pct)
            / relative_volume
            if relative_volume > 0
            else None
        )

        efficiency_3m = (
            abs(move_3m_pct)
            / volume_3m_ratio
            if (
                move_3m_pct is not None
                and volume_3m_ratio
                and volume_3m_ratio > 0
            )
            else None
        )

        efficiency_5m = (
            abs(move_5m_pct)
            / volume_5m_ratio
            if (
                move_5m_pct is not None
                and volume_5m_ratio
                and volume_5m_ratio > 0
            )
            else None
        )

        # ======================================
        # EVENT ID + TIMESTAMPS
        # ======================================

        event_id = (
            f"{symbol}_"
            f"{self.timeframe}_"
            f"{current_timestamp}"
        )

        candle_open_time_utc = (
            self._timestamp_to_iso(
                current_timestamp
            )
        )

        candle_close_time_utc = (
            self._timestamp_to_iso(
                close_time
            )
        )

        # ======================================
        # SWING CONTEXT
        # ======================================

        swing_contexts = (
            self._build_all_swing_contexts(
                symbol=symbol,
                event_timestamp=close_time,
                event_price=close_price,
            )
        )

        # ======================================
        # EVENT
        # ======================================

        event = {
            # Identity
            "event_id": event_id,
            "symbol": symbol,
            "timeframe": self.timeframe,

            # Raw timestamps
            "candle_open_timestamp": (
                current_timestamp
            ),
            "candle_close_timestamp": (
                close_time
            ),

            # Human-readable timestamps
            "candle_open_time_utc": (
                candle_open_time_utc
            ),
            "candle_close_time_utc": (
                candle_close_time_utc
            ),

            # Price
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,

            # Volume
            "volume": current_volume,
            "baseline_volume": (
                baseline_volume
            ),
            "relative_volume": (
                relative_volume
            ),

            "volume_2m_ratio": (
                volume_2m_ratio
            ),
            "volume_3m_ratio": (
                volume_3m_ratio
            ),
            "volume_4m_ratio": (
                volume_4m_ratio
            ),
            "volume_5m_ratio": (
                volume_5m_ratio
            ),

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
            "upper_wick_pct": (
                upper_wick_pct
            ),
            "lower_wick_pct": (
                lower_wick_pct
            ),
            "close_location": (
                close_location
            ),
            "candle_direction": (
                candle_direction
            ),

            # Momentum
            "rsi_1m": rsi_1m,

            # Efficiency
            "price_volume_efficiency": (
                price_volume_efficiency
            ),
            "efficiency_3m": (
                efficiency_3m
            ),
            "efficiency_5m": (
                efficiency_5m
            ),
        }

        # ======================================
        # ADD SWINGS TO EVENT
        # ======================================

        for timeframe in self.SWING_TIMEFRAMES:
            context = swing_contexts.get(
                timeframe,
                {},
            )

            event[
                f"swing_low_{timeframe}"
            ] = context.get(
                "swing_low"
            )

            event[
                f"swing_low_{timeframe}_ts"
            ] = context.get(
                "swing_low_ts"
            )

            event[
                f"swing_low_{timeframe}_time_utc"
            ] = context.get(
                "swing_low_time_utc"
            )

            event[
                f"swing_high_{timeframe}"
            ] = context.get(
                "swing_high"
            )

            event[
                f"swing_high_{timeframe}_ts"
            ] = context.get(
                "swing_high_ts"
            )

            event[
                f"swing_high_{timeframe}_time_utc"
            ] = context.get(
                "swing_high_time_utc"
            )

            event[
                f"dist_swing_low_{timeframe}_pct"
            ] = context.get(
                "dist_swing_low_pct"
            )

            event[
                f"dist_swing_high_{timeframe}_pct"
            ] = context.get(
                "dist_swing_high_pct"
            )

            event[
                f"near_swing_low_{timeframe}"
            ] = context.get(
                "near_swing_low"
            )

            event[
                f"near_swing_high_{timeframe}"
            ] = context.get(
                "near_swing_high"
            )

            event[
                f"swing_atr_{timeframe}"
            ] = context.get(
                "swing_atr"
            )

            event[
                f"swing_near_mult_{timeframe}"
            ] = context.get(
                "swing_near_mult"
            )

        # ======================================
        # PERSIST
        # ======================================

        self._save_event(event)

        self.events_detected += 1

        return event