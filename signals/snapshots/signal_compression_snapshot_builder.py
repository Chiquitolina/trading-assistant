import time
import pandas as pd

from models.signal_compression_snapshot import SignalCompressionSnapshot

class SignalCompressionSnapshotBuilder:

    def __init__(self, buffer):
        self.buffer = buffer


    def _candle_progress_pct(self, df: pd.DataFrame | None) -> float | None:
        if df is None or df.empty:
            return None

        if "timestamp" not in df.columns:
            return None

        last_ts = int(df.iloc[-1]["timestamp"])

        # 15m en ms
        tf_ms = 15 * 60 * 1000

        now_ms = int(time.time() * 1000)

        progress = ((now_ms - last_ts) / tf_ms) * 100

        if progress < 0:
            return 0.0

        if progress > 100:
            return 100.0

        return round(progress, 2)

    def _btc_metrics(
        self,
        symbol: str,
        df_symbol: pd.DataFrame | None,
        tf: str = "1h",
        lookback: int = 168,  # 7 días de velas 1h
    ):
        if symbol == "BTCUSDT":
            return None, None, None, None, None

        df_btc = self._df("BTCUSDT", tf)

        if df_symbol is None or df_btc is None:
            return None, None, None, None, None

        if len(df_symbol) < lookback or len(df_btc) < lookback:
            return None, None, None, None, None

        if "close" not in df_symbol.columns or "close" not in df_btc.columns:
            return None, None, None, None, None

        s = df_symbol[["timestamp", "close"]].tail(lookback).copy()
        b = df_btc[["timestamp", "close"]].tail(lookback).copy()

        s = s.rename(columns={"close": "symbol_close"})
        b = b.rename(columns={"close": "btc_close"})

        merged = pd.merge(
            s,
            b,
            on="timestamp",
            how="inner"
        )

        if len(merged) < 30:
            return None, None, None, None, None

        merged["symbol_ret"] = merged["symbol_close"].pct_change()
        merged["btc_ret"] = merged["btc_close"].pct_change()

        merged = merged.dropna(subset=["symbol_ret", "btc_ret"])

        if len(merged) < 30:
            return None, None, None, None, None

        symbol_ret = merged["symbol_ret"]
        btc_ret = merged["btc_ret"]

        btc_var = btc_ret.var()

        if btc_var == 0:
            return None, None, None, None, None

        corr = symbol_ret.corr(btc_ret)
        beta = symbol_ret.cov(btc_ret) / btc_var
        r2 = corr ** 2 if corr is not None else None

        symbol_vol = symbol_ret.std()
        btc_vol = btc_ret.std()

        vol_ratio = (
            symbol_vol / btc_vol
            if btc_vol and btc_vol > 0
            else None
        )

        symbol_return_7d = (
            merged["symbol_close"].iloc[-1]
            / merged["symbol_close"].iloc[0]
            - 1
        ) * 100

        btc_return_7d = (
            merged["btc_close"].iloc[-1]
            / merged["btc_close"].iloc[0]
            - 1
        ) * 100

        outperformance = symbol_return_7d - btc_return_7d

        return (
            round(float(corr), 4) if corr is not None else None,
            round(float(beta), 4) if beta is not None else None,
            round(float(r2), 4) if r2 is not None else None,
            round(float(vol_ratio), 4) if vol_ratio is not None else None,
            round(float(outperformance), 4),
        )

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
        snapshot.candle_progress_pct = self._candle_progress_pct(df_15m)

        # ==========================
        # COMPRESSION
        # ==========================
        (
            snapshot.compression_score,
            snapshot.compression_high,
            snapshot.compression_low,
            snapshot.range_ratio_15m,
            snapshot.atr_ratio_15m,
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
        
        (
            snapshot.btc_corr_7d,
            snapshot.beta_vs_btc,
            snapshot.r2_vs_btc,
            snapshot.vol_ratio_vs_btc,
            snapshot.outperformance_7d,
        ) = self._btc_metrics(symbol, df_1h)

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
            # EMAs
            df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
            df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

            # RSI 14
            delta = df["close"].diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)

            avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()

            rs = avg_gain / avg_loss
            df["rsi"] = 100 - (100 / (1 + rs))

            # MACD 12/26/9
            ema12 = df["close"].ewm(span=12, adjust=False).mean()
            ema26 = df["close"].ewm(span=26, adjust=False).mean()

            df["macd"] = ema12 - ema26
            df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
            df["macd_hist"] = df["macd"] - df["macd_signal"]

        if {"high", "low", "close"}.issubset(df.columns):
            # ADX 14
            high = df["high"]
            low = df["low"]
            close = df["close"]

            prev_close = close.shift(1)

            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ], axis=1).max(axis=1)

            up_move = high.diff()
            down_move = -low.diff()

            plus_dm = up_move.where(
                (up_move > down_move) & (up_move > 0),
                0.0
            )

            minus_dm = down_move.where(
                (down_move > up_move) & (down_move > 0),
                0.0
            )

            atr = tr.ewm(alpha=1 / 14, adjust=False).mean()

            plus_di = (
                100
                * plus_dm.ewm(alpha=1 / 14, adjust=False).mean()
                / atr
            )

            minus_di = (
                100
                * minus_dm.ewm(alpha=1 / 14, adjust=False).mean()
                / atr
            )

            dx = (
                (plus_di - minus_di).abs()
                / (plus_di + minus_di)
            ) * 100

            df["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
            df["plus_di"] = plus_di
            df["minus_di"] = minus_di

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
    
    def _atr_ratio(
        self,
        df: pd.DataFrame | None,
        lookback: int = 10,
        base_lookback: int = 40,
    ) -> float | None:

        if df is None or len(df) < base_lookback + 1:
            return None

        if not {"high", "low", "close"}.issubset(df.columns):
            return None

        atr = (
            df["high"] - df["low"]
        ).rolling(14).mean()

        recent_atr = atr.iloc[-lookback - 1:-1].mean()
        base_atr = atr.iloc[-base_lookback - 1:-1].mean()

        if pd.isna(recent_atr) or pd.isna(base_atr) or base_atr <= 0:
            return None

        return round(float(recent_atr / base_atr), 4)

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
            return None, None, None, None, None

        if not {"high", "low", "volume"}.issubset(df.columns):
            return None, None, None, None, None

        recent = df.iloc[-lookback - 1:-1]
        base = df.iloc[-base_lookback - 1:-1]

        recent_range = float(
            recent["high"].max() - recent["low"].min()
        )

        base_range = float(
            base["high"].max() - base["low"].min()
        )

        if base_range <= 0:
            return None, None, None, None, None

        range_ratio = recent_range / base_range
        volume_ratio = self._volume_ratio(df)
        atr_ratio = self._atr_ratio(df)

        score = 0

        # Range compression
        if range_ratio < 0.35:
            score += 2
        elif range_ratio < 0.50:
            score += 1

        # ATR compression
        if atr_ratio is not None:
            if atr_ratio < 0.70:
                score += 2
            elif atr_ratio < 0.85:
                score += 1

        # Volume compression
        if volume_ratio is not None and volume_ratio < 0.75:
            score += 1

        compression_high = float(recent["high"].max())
        compression_low = float(recent["low"].min())

        return (
            score,
            compression_high,
            compression_low,
            round(range_ratio, 4),
            atr_ratio,
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