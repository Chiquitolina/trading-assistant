from dataclasses import dataclass, field
from typing import Any, Optional

import json

from datetime import timedelta

import requests

import pandas as pd


@dataclass
class NormalizedTrade:
    symbol: str
    status: str
    side: str

    entry_ts: Optional[pd.Timestamp]
    entry_price: Optional[float]

    tp: Optional[float]
    sl: Optional[float]

    exit_ts: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    compression_start_ts: Optional[pd.Timestamp] = None
    compression_created_ts: Optional[pd.Timestamp] = None
    compression_updated_ts: Optional[pd.Timestamp] = None

    compression_high: Optional[float] = None
    compression_low: Optional[float] = None
    compression_score: Optional[float] = None
    
    compression_duration: Optional[int] = None

    breakout_ts: Optional[pd.Timestamp] = None
    breakout_price: Optional[float] = None
    breakout_high: Optional[float] = None

    pullback_first_ts: Optional[pd.Timestamp] = None
    pullback_valid_ts: Optional[pd.Timestamp] = None
    pullback_price: Optional[float] = None

    entry_ready_ts: Optional[pd.Timestamp] = None
    entry_ready_price: Optional[float] = None

    current_price: Optional[float] = None
    pnl_pct: Optional[float] = None
    pnl_usd: Optional[float] = None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeInspection:
    trade: NormalizedTrade
    candles: pd.DataFrame = field(default_factory=pd.DataFrame)
    events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    diagnosis: dict[str, Any] = field(default_factory=dict)


class TradeInspectorService:
    
    BINANCE_FUTURES_KLINES_URL = (
        "https://fapi.binance.com/fapi/v1/klines"
    )

    KLINE_COLUMNS = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trade_count",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore",
    ]

    def normalize_trade(
        self,
        row: dict[str, Any] | pd.Series,
        status: str,
    ) -> NormalizedTrade:

        raw = self._to_dict(row)
        context = self._extract_signal_context(raw)

        return NormalizedTrade(
            symbol=str(
                self._first_value(
                    raw,
                    context,
                    "symbol",
                    default="",
                )
            ).upper(),

            status=str(status).upper(),

            side=str(
                self._first_value(
                    raw,
                    context,
                    "side",
                    "position_side",
                    default="",
                )
            ).upper(),

            entry_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "entry_ts",
                    "opened_at",
                    "open_ts",
                )
            ),

            entry_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "real_entry",
                    "entry_price",
                    "entry",
                )
            ),

            tp=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "tp",
                    "take_profit",
                )
            ),

            sl=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "sl",
                    "stop_loss",
                )
            ),

            exit_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "exit_ts",
                    "closed_at",
                    "close_ts",
                )
            ),

            exit_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "real_exit",
                    "exit_price",
                    "exit",
                )
            ),

            exit_reason=self._to_optional_string(
                self._first_value(
                    raw,
                    context,
                    "exit_reason",
                    "reason",
                )
            ),

            compression_start_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "compression_start_ts",
                )
            ),

            compression_created_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "compression_created_ts",
                )
            ),

            compression_updated_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "compression_updated_ts",
                )
            ),

            compression_high=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "compression_high",
                )
            ),

            compression_low=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "compression_low",
                )
            ),

            compression_score=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "compression_score",
                )
            ),
            
            compression_duration=self._to_int(
                self._first_value(
                    raw,
                    context,
                    "compression_duration",
                )
            ),

            breakout_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "breakout_ts",
                )
            ),

            breakout_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "breakout_price",
                )
            ),

            breakout_high=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "breakout_high",
                )
            ),

            pullback_first_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "pullback_first_ts",
                )
            ),

            pullback_valid_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "pullback_valid_ts",
                )
            ),

            pullback_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "pullback_price",
                )
            ),

            entry_ready_ts=self._to_timestamp(
                self._first_value(
                    raw,
                    context,
                    "entry_ready_ts",
                )
            ),

            entry_ready_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "entry_ready_price",
                )
            ),

            current_price=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "mark_price",
                    "current_price",
                )
            ),

            pnl_pct=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "pnl",
                    "current_pnl",
                )
            ),

            pnl_usd=self._to_float(
                self._first_value(
                    raw,
                    context,
                    "pnl_usd",
                    "unrealized_pnl",
                    "unpnl",
                )
            ),

            raw=raw,
        )
        
    def load_trade_candles(
        self,
        trade: NormalizedTrade,
        interval: str = "30m",
        candles_before: int = 12,
        candles_after: int = 8,
    ) -> pd.DataFrame:

        if not trade.symbol:
            return pd.DataFrame()

        interval_minutes = self._interval_to_minutes(
            interval
        )

        reference_timestamps = [
            trade.compression_start_ts,
            trade.compression_created_ts,
            trade.breakout_ts,
            trade.pullback_first_ts,
            trade.pullback_valid_ts,
            trade.entry_ready_ts,
            trade.entry_ts,
        ]

        valid_start_timestamps = [
            ts
            for ts in reference_timestamps
            if ts is not None
        ]

        if not valid_start_timestamps:
            return pd.DataFrame()

        first_event_ts = min(valid_start_timestamps)

        start_ts = first_event_ts - timedelta(
            minutes=interval_minutes * candles_before
        )

        if trade.exit_ts is not None:
            last_event_ts = trade.exit_ts
        else:
            last_event_ts = pd.Timestamp.now(tz="UTC")

        end_ts = last_event_ts + timedelta(
            minutes=interval_minutes * candles_after
        )

        return self.fetch_klines(
            symbol=trade.symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        
    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
    ) -> pd.DataFrame:

        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": int(start_ts.timestamp() * 1000),
            "endTime": int(end_ts.timestamp() * 1000),
            "limit": 1500,
        }

        try:
            response = requests.get(
                self.BINANCE_FUTURES_KLINES_URL,
                params=params,
                timeout=10,
            )

            response.raise_for_status()
            raw_klines = response.json()

        except requests.RequestException:
            return pd.DataFrame()

        if not raw_klines:
            return pd.DataFrame()

        candles = pd.DataFrame(
            raw_klines,
            columns=self.KLINE_COLUMNS,
        )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base",
            "taker_buy_quote",
        ]

        for column in numeric_columns:
            candles[column] = pd.to_numeric(
                candles[column],
                errors="coerce",
            )

        candles["open_ts"] = pd.to_datetime(
            candles["open_time"],
            unit="ms",
            utc=True,
            errors="coerce",
        )

        candles["close_ts"] = pd.to_datetime(
            candles["close_time"],
            unit="ms",
            utc=True,
            errors="coerce",
        )

        candles = (
            candles
            .dropna(
                subset=[
                    "open_ts",
                    "open",
                    "high",
                    "low",
                    "close",
                ]
            )
            .sort_values("open_ts")
            .drop_duplicates(
                subset=["open_ts"],
                keep="last",
            )
            .reset_index(drop=True)
        )

        return candles

    def inspect(
        self,
        row: dict[str, Any] | pd.Series,
        status: str,
    ) -> TradeInspection:

        trade = self.normalize_trade(
            row=row,
            status=status,
        )

        return TradeInspection(
            trade=trade,
        )
        
    @staticmethod
    def _interval_to_minutes(
        interval: str,
    ) -> int:

        interval_map = {
            "1m": 1,
            "3m": 3,
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "2h": 120,
            "4h": 240,
        }

        if interval not in interval_map:
            raise ValueError(
                f"Unsupported candle interval: {interval}"
            )

        return interval_map[interval]

    @staticmethod
    def _to_dict(
        row: dict[str, Any] | pd.Series,
    ) -> dict[str, Any]:

        if isinstance(row, pd.Series):
            return row.to_dict()

        return dict(row)

    @staticmethod
    def _extract_signal_context(
        raw: dict[str, Any],
    ) -> dict[str, Any]:

        context = raw.get("signal_context")

        if isinstance(context, dict):
            return context

        if isinstance(context, str):
            try:
                parsed = json.loads(context)

                if isinstance(parsed, dict):
                    return parsed

            except Exception:
                pass

        return {}

    @staticmethod
    def _first_value(
        raw: dict[str, Any],
        context: dict[str, Any],
        *keys: str,
        default=None,
    ):

        for source in (raw, context):
            for key in keys:
                value = source.get(key)

                if value is None:
                    continue

                try:
                    if pd.isna(value):
                        continue
                except (TypeError, ValueError):
                    pass

                if value == "":
                    continue

                return value

        return default

    @staticmethod
    def _to_float(value) -> Optional[float]:

        if value is None:
            return None

        try:
            number = float(value)

            if pd.isna(number):
                return None

            return number

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_timestamp(value) -> Optional[pd.Timestamp]:

        if value is None:
            return None

        # ==========================================
        # NUMERIC TIMESTAMPS
        # ==========================================

        numeric_value = None

        if isinstance(value, (int, float)):
            numeric_value = float(value)

        elif isinstance(value, str):
            stripped = value.strip()

            try:
                numeric_value = float(stripped)
            except ValueError:
                numeric_value = None

        if numeric_value is not None:

            if pd.isna(numeric_value):
                return None

            absolute_value = abs(numeric_value)

            # Nanoseconds: 1780000000000000000
            if absolute_value >= 1e17:
                unit = "ns"

            # Microseconds: 1780000000000000
            elif absolute_value >= 1e14:
                unit = "us"

            # Milliseconds: 1780000000000
            elif absolute_value >= 1e11:
                unit = "ms"

            # Seconds: 1780000000
            elif absolute_value >= 1e9:
                unit = "s"

            else:
                return None

            timestamp = pd.to_datetime(
                numeric_value,
                unit=unit,
                utc=True,
                errors="coerce",
            )

        # ==========================================
        # ISO / DATETIME VALUES
        # ==========================================

        else:
            timestamp = pd.to_datetime(
                value,
                utc=True,
                errors="coerce",
            )

        if pd.isna(timestamp):
            return None

        return timestamp

    @staticmethod
    def _to_optional_string(value) -> Optional[str]:

        if value is None:
            return None

        text = str(value).strip()

        return text or None

    @staticmethod
    def _to_int(value) -> Optional[int]:

        if value is None:
            return None

        try:
            number = float(value)

            if pd.isna(number):
                return None

            return int(number)

        except (TypeError, ValueError):
            return None