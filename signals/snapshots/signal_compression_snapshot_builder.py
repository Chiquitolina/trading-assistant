import time
import pandas as pd

from models.signal_compression_snapshot import SignalCompressionSnapshot


class SignalCompressionSnapshotBuilder:

    def __init__(self, buffer):
        self.buffer = buffer

    def build(self, symbol: str) -> SignalCompressionSnapshot | None:

        df_15m = self._df(symbol, "15m")
        df_1h = self._df(symbol, "1h")
        df_4h = self._df(symbol, "4h")

        if df_15m is None or df_15m.empty:
            return None

        snapshot = SignalCompressionSnapshot(
            symbol=symbol,
            timestamp=int(time.time() * 1000),
        )

        # ==========================
        # TREND
        # ==========================
        snapshot.trend_15m = self._trend(df_15m)
        snapshot.trend_1h = self._trend(df_1h)
        snapshot.trend_4h = self._trend(df_4h)

        # ==========================
        # MOMENTUM
        # ==========================
        snapshot.rsi_1h = self._last(df_1h, "rsi")
        snapshot.adx_1h = self._last(df_1h, "adx")
        snapshot.macd_hist_1h = self._last(df_1h, "macd_hist")

        # ==========================
        # VOLUME
        # ==========================
        snapshot.volume_ratio_15m = self._volume_ratio(df_15m)
        snapshot.volume_ratio_1h = self._volume_ratio(df_1h)

        # ==========================
        # STRUCTURE
        # ==========================
        snapshot.move_3bars_pct = self._move_pct(df_15m, bars=3)
        snapshot.dist_ema20_1h_pct = self._dist_ema_pct(df_1h, "ema20")

        # ==========================
        # COMPRESSION
        # ==========================
        (
            snapshot.compression_score,
            snapshot.compression_high,
            snapshot.compression_low,
        ) = self._compression(df_15m)

        # ==========================
        # BREAKOUT
        # ==========================
        (
            snapshot.breakout_side,
            snapshot.breakout_score,
        ) = self._breakout(
            df_15m,
            snapshot.compression_high,
            snapshot.compression_low,
        )

        # ==========================
        # TAGS
        # ==========================
        snapshot.tags = self._build_tags(snapshot)

        return snapshot

    # ==========================================================
    # HELPERS
    # ==========================================================
    
    def _df(self, symbol: str, tf: str) -> pd.DataFrame | None:
        candles = self.buffer.get_candles(symbol, tf)

        if not candles:
            return None

        df = pd.DataFrame(candles)

        if "close" in df.columns:
            df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
            df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

        return df

    def _last(self, df: pd.DataFrame | None, col: str):

        if df is None or df.empty:
            return None

        if col not in df.columns:
            return None

        value = df.iloc[-1][col]

        if pd.isna(value):
            return None

        return float(value)

    def _trend(self, df: pd.DataFrame | None):

        if df is None or df.empty:
            return None

        if "ema20" not in df.columns or "ema50" not in df.columns:
            return None

        last = df.iloc[-1]

        if last["ema20"] > last["ema50"]:
            return "BULLISH"

        if last["ema20"] < last["ema50"]:
            return "BEARISH"

        return "RANGING"

    def _volume_ratio(
        self,
        df: pd.DataFrame | None,
        lookback: int = 20,
    ):

        if df is None or len(df) < lookback + 1:
            return None

        if "volume" not in df.columns:
            return None

        last_vol = float(df.iloc[-1]["volume"])
        avg_vol = float(df["volume"].iloc[-lookback - 1:-1].mean())

        if avg_vol <= 0:
            return None

        return round(last_vol / avg_vol, 2)

    def _move_pct(
        self,
        df: pd.DataFrame | None,
        bars: int = 3,
    ):

        if df is None or len(df) < bars + 1:
            return None

        old = float(df.iloc[-bars - 1]["close"])
        new = float(df.iloc[-1]["close"])

        if old <= 0:
            return None

        return round(
            ((new - old) / old) * 100,
            4,
        )

    def _dist_ema_pct(
        self,
        df: pd.DataFrame | None,
        ema_col: str,
    ):

        if df is None or df.empty:
            return None

        if ema_col not in df.columns:
            return None

        close = float(df.iloc[-1]["close"])
        ema = float(df.iloc[-1][ema_col])

        if ema <= 0:
            return None

        return round(
            ((close - ema) / ema) * 100,
            4,
        )

    # ==========================================================
    # COMPRESSION
    # ==========================================================

    def _compression(
        self,
        df: pd.DataFrame | None,
        lookback: int = 10,
        base_lookback: int = 40,
    ):

        if df is None or len(df) < base_lookback + 1:
            return None, None, None

        if not {"high", "low", "volume"}.issubset(df.columns):
            return None, None, None

        # últimas 10 velas previas
        recent = df.iloc[-lookback - 1:-1]

        # últimas 40 velas previas
        base = df.iloc[-base_lookback - 1:-1]

        recent_range = float(
            recent["high"].max() - recent["low"].min()
        )

        base_range = float(
            base["high"].max() - base["low"].min()
        )

        if base_range <= 0:
            return None, None, None

        range_ratio = recent_range / base_range

        score = 0

        if range_ratio < 0.35:
            score += 2
        elif range_ratio < 0.50:
            score += 1

        volume_ratio = self._volume_ratio(df)

        if volume_ratio is not None and volume_ratio < 0.75:
            score += 1

        compression_high = float(recent["high"].max())
        compression_low = float(recent["low"].min())

        return (
            score,
            compression_high,
            compression_low,
        )

    # ==========================================================
    # BREAKOUT
    # ==========================================================

    def _breakout(
        self,
        df: pd.DataFrame | None,
        compression_high: float | None,
        compression_low: float | None,
    ):

        if df is None or df.empty:
            return None, 0

        if compression_high is None or compression_low is None:
            return None, 0

        last = df.iloc[-1]

        close = float(last["close"])

        score = 0

        if close > compression_high:
            score += 1
            return "UP", score

        if close < compression_low:
            score += 1
            return "DOWN", score

        return None, 0

    # ==========================================================
    # TAGS
    # ==========================================================

    def _build_tags(
        self,
        s: SignalCompressionSnapshot,
    ) -> list[str]:

        tags = []

        if s.trend_1h == "BULLISH":
            tags.append("H1_BULLISH")

        if s.trend_1h == "BEARISH":
            tags.append("H1_BEARISH")

        if (
            s.compression_score is not None
            and s.compression_score >= 2
        ):
            tags.append("COMPRESSION")

        if s.breakout_side == "UP":
            tags.append("BREAKOUT_UP")

        if s.breakout_side == "DOWN":
            tags.append("BREAKOUT_DOWN")

        if (
            s.volume_ratio_15m is not None
            and s.volume_ratio_15m >= 2
        ):
            tags.append("VOL_SPIKE_15M")

        if (
            s.move_3bars_pct is not None
            and abs(s.move_3bars_pct) >= 2
        ):
            tags.append("STRONG_MOVE_3BARS")

        return tags