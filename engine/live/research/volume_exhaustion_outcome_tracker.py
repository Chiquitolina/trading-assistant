import csv
import os
from datetime import datetime, timezone


class VolumeExhaustionOutcomeTracker:
    """
    Research-only tracker.

    Sigue qué ocurre DESPUÉS de cada Volume Exhaustion Event.

    Para cada evento mide:

    - Return a +1m, +3m, +5m, +10m, +15m, +30m
    - MFE acumulado por horizonte
    - MAE acumulado por horizonte
    - Tiempo hasta niveles favorables
    - Tiempo hasta niveles adversos

    LONG:
        favorable = precio sube
        adverse   = precio baja

    SHORT:
        favorable = precio baja
        adverse   = precio sube

    No genera señales.
    No ejecuta órdenes.
    """

    HORIZONS = (
        1,
        3,
        5,
        10,
        15,
        30,
    )

    LEVELS = (
        0.25,
        0.50,
        0.75,
        1.00,
    )

    def __init__(
        self,
        outcomes_path="volume_exhaustion_outcomes.csv",
    ):
        self.outcomes_path = outcomes_path

        # event_id -> estado del evento
        self.active_events = {}

        self.completed_events = 0

    # ==========================================
    # HELPERS
    # ==========================================

    @staticmethod
    def _timestamp_to_iso(timestamp_ms):
        return datetime.fromtimestamp(
            int(timestamp_ms) / 1000,
            tz=timezone.utc,
        ).isoformat()

    @staticmethod
    def _potential_side(event):
        """
        Clasificación inicial de research.

        Movimiento previo bajista:
            posible exhaustion vendedor -> LONG

        Movimiento previo alcista:
            posible exhaustion comprador -> SHORT
        """

        move_3m = event.get("move_3m_pct")

        if move_3m is None:
            return None

        move_3m = float(move_3m)

        if move_3m < 0:
            return "LONG"

        if move_3m > 0:
            return "SHORT"

        return None

    @staticmethod
    def _signed_return(
        entry_price,
        price,
        side,
    ):
        """
        Retorno desde la perspectiva del setup.

        LONG:
            subida = positivo

        SHORT:
            bajada = positivo
        """

        entry_price = float(entry_price)
        price = float(price)

        if entry_price <= 0:
            return None

        raw_pct = (
            (price / entry_price) - 1
        ) * 100

        if side == "LONG":
            return raw_pct

        if side == "SHORT":
            return -raw_pct

        return None

    # ==========================================
    # REGISTER
    # ==========================================

    def register(self, event):
        """
        Registra un nuevo evento T0.

        IMPORTANTE:
        La vela del propio evento NO se utiliza
        como outcome.
        """

        event_id = str(
            event["event_id"]
        )

        if event_id in self.active_events:
            return False

        side = self._potential_side(event)

        if side is None:
            return False

        entry_price = float(
            event["close"]
        )

        if entry_price <= 0:
            return False

        event_open_timestamp = int(
            event["candle_open_timestamp"]
        )

        event_close_timestamp = int(
            event["candle_close_timestamp"]
        )

        state = {
            "event_id": event_id,
            "symbol": str(
                event["symbol"]
            ).upper(),
            "timeframe": event.get(
                "timeframe",
                "1m",
            ),
            "potential_side": side,

            "event_open_timestamp": (
                event_open_timestamp
            ),
            "event_close_timestamp": (
                event_close_timestamp
            ),

            "event_open_time_utc": (
                event.get(
                    "candle_open_time_utc"
                )
                or self._timestamp_to_iso(
                    event_open_timestamp
                )
            ),

            "event_close_time_utc": (
                event.get(
                    "candle_close_time_utc"
                )
                or self._timestamp_to_iso(
                    event_close_timestamp
                )
            ),

            "entry_price": entry_price,

            # Cuántas velas posteriores
            # ya observamos.
            "age_candles": 0,

            # Extremos acumulados desde T+1.
            "max_high": None,
            "min_low": None,

            # Outcomes por horizonte.
            "returns": {},
            "mfe": {},
            "mae": {},

            # First-touch favorable/adverse.
            "favorable_times": {
                level: None
                for level in self.LEVELS
            },

            "adverse_times": {
                level: None
                for level in self.LEVELS
            },

            # Para evitar procesar dos veces
            # el mismo boundary.
            "last_processed_close_time": None,
        }

        self.active_events[
            event_id
        ] = state

        return True

    # ==========================================
    # CANDLE UPDATE
    # ==========================================

    def on_candle(
        self,
        symbol,
        close_time,
        candle,
    ):
        """
        Procesa UNA vela 1m recién cerrada.

        Debe llamarse ANTES de registrar
        posibles eventos nuevos producidos
        por esa misma vela.
        """

        symbol = str(symbol).upper()
        close_time = int(close_time)

        if candle is None:
            return []

        completed = []

        # Copia porque podemos eliminar
        # eventos durante la iteración.
        for event_id, state in list(
            self.active_events.items()
        ):
            if state["symbol"] != symbol:
                continue

            # Nunca contamos T0.
            if (
                close_time
                <= state["event_close_timestamp"]
            ):
                continue

            last_processed = state[
                "last_processed_close_time"
            ]

            if (
                last_processed is not None
                and close_time <= last_processed
            ):
                continue

            self._update_event(
                state=state,
                close_time=close_time,
                candle=candle,
            )

            state[
                "last_processed_close_time"
            ] = close_time

            if (
                state["age_candles"]
                >= max(self.HORIZONS)
            ):
                row = self._build_row(
                    state
                )

                self._save_outcome(
                    row
                )

                completed.append(row)

                del self.active_events[
                    event_id
                ]

                self.completed_events += 1

        return completed

    # ==========================================
    # EVENT UPDATE
    # ==========================================

    def _update_event(
        self,
        state,
        close_time,
        candle,
    ):
        state["age_candles"] += 1

        age = state["age_candles"]

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        close = float(
            candle["close"]
        )

        if state["max_high"] is None:
            state["max_high"] = high
        else:
            state["max_high"] = max(
                state["max_high"],
                high,
            )

        if state["min_low"] is None:
            state["min_low"] = low
        else:
            state["min_low"] = min(
                state["min_low"],
                low,
            )

        side = state[
            "potential_side"
        ]

        entry = state[
            "entry_price"
        ]

        # ======================================
        # CURRENT CLOSE RETURN
        # ======================================

        current_return = (
            self._signed_return(
                entry,
                close,
                side,
            )
        )

        # ======================================
        # CURRENT MFE / MAE
        # ======================================

        if side == "LONG":

            mfe = self._signed_return(
                entry,
                state["max_high"],
                "LONG",
            )

            mae_raw = self._signed_return(
                entry,
                state["min_low"],
                "LONG",
            )

        else:

            mfe = self._signed_return(
                entry,
                state["min_low"],
                "SHORT",
            )

            mae_raw = self._signed_return(
                entry,
                state["max_high"],
                "SHORT",
            )

        # MAE lo guardamos positivo:
        #
        # mae = 0.42
        #
        # significa:
        # llegó 0.42% en contra.
        mae = max(
            0.0,
            -float(mae_raw),
        )

        mfe = max(
            0.0,
            float(mfe),
        )

        # ======================================
        # FIRST TOUCH LEVELS
        # ======================================

        for level in self.LEVELS:

            if (
                state[
                    "favorable_times"
                ][level]
                is None
                and mfe >= level
            ):
                state[
                    "favorable_times"
                ][level] = age

            if (
                state[
                    "adverse_times"
                ][level]
                is None
                and mae >= level
            ):
                state[
                    "adverse_times"
                ][level] = age

        # ======================================
        # SNAPSHOT AT HORIZON
        # ======================================

        if age in self.HORIZONS:

            state["returns"][
                age
            ] = current_return

            state["mfe"][
                age
            ] = mfe

            state["mae"][
                age
            ] = mae

    # ==========================================
    # BUILD FINAL ROW
    # ==========================================

    def _build_row(
        self,
        state,
    ):
        row = {
            "event_id": (
                state["event_id"]
            ),
            "symbol": (
                state["symbol"]
            ),
            "timeframe": (
                state["timeframe"]
            ),
            "potential_side": (
                state["potential_side"]
            ),

            "event_open_timestamp": (
                state[
                    "event_open_timestamp"
                ]
            ),
            "event_close_timestamp": (
                state[
                    "event_close_timestamp"
                ]
            ),

            "event_open_time_utc": (
                state[
                    "event_open_time_utc"
                ]
            ),
            "event_close_time_utc": (
                state[
                    "event_close_time_utc"
                ]
            ),

            "entry_price": (
                state["entry_price"]
            ),
        }

        # ======================================
        # HORIZONS
        # ======================================

        for horizon in self.HORIZONS:

            row[
                f"return_{horizon}m_pct"
            ] = state[
                "returns"
            ].get(horizon)

            row[
                f"mfe_{horizon}m_pct"
            ] = state[
                "mfe"
            ].get(horizon)

            row[
                f"mae_{horizon}m_pct"
            ] = state[
                "mae"
            ].get(horizon)

        # ======================================
        # FIRST TOUCH
        # ======================================

        for level in self.LEVELS:

            label = (
                f"{level:.2f}"
                .replace(".", "_")
            )

            row[
                f"time_to_favorable_{label}_pct_min"
            ] = state[
                "favorable_times"
            ][level]

            row[
                f"time_to_adverse_{label}_pct_min"
            ] = state[
                "adverse_times"
            ][level]

        row[
            "completed_timestamp"
        ] = int(
            datetime.now(
                tz=timezone.utc
            ).timestamp() * 1000
        )

        row[
            "completed_time_utc"
        ] = datetime.now(
            tz=timezone.utc
        ).isoformat()

        return row

    # ==========================================
    # CSV
    # ==========================================

    def _save_outcome(
        self,
        row,
    ):
        file_exists = os.path.exists(
            self.outcomes_path
        )

        fieldnames = list(
            row.keys()
        )

        with open(
            self.outcomes_path,
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

            writer.writerow(row)

    # ==========================================
    # STATUS
    # ==========================================

    def active_count(self):
        return len(
            self.active_events
        )