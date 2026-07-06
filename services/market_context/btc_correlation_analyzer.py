from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class BTCCorrelationAnalyzer:
    """
    Calcula métricas de relación entre un símbolo y BTC.

    No bloquea trades.
    No aplica scoring.
    Solo devuelve datos para guardarlos y analizarlos.
    """

    DEFAULT_CONFIG = {
        "15m": {
            "correlation_lookback": 96,
            "move_lookback": 4,
        },
        "1h": {
            "correlation_lookback": 72,
            "move_lookback": 3,
        },
        "4h": {
            "correlation_lookback": 42,
            "move_lookback": 2,
        },
    }
    
    RECENT_CONFIG = {
        "5m_1h": {
            "timeframe": "5m",
            "correlation_lookback": 12,
        },
        "5m_4h": {
            "timeframe": "5m",
            "correlation_lookback": 48,
        },
        "5m_24h": {
            "timeframe": "5m",
            "correlation_lookback": 288,
        },
    }

    PAIR_CORRELATION_THRESHOLD = 0.70

    def __init__(
        self,
        buffer,
        btc_symbol: str = "BTCUSDT",
        timeframe_config: dict | None = None,
        recent_config: dict | None = None,
        pair_correlation_threshold: float = 0.70,
    ):
        self.buffer = buffer
        self.btc_symbol = btc_symbol

        self.timeframe_config = (
            timeframe_config or self.DEFAULT_CONFIG
        )

        self.recent_config = (
            recent_config or self.RECENT_CONFIG
        )

        self.pair_correlation_threshold = float(
            pair_correlation_threshold
        )

    def analyze(self, symbol: str) -> dict[str, Any]:
        """
        Devuelve un diccionario plano listo para incorporar
        a TradePlan.signal_context y luego a trades.csv.
        """

        if symbol == self.btc_symbol:
            return self._btc_result()

        result: dict[str, Any] = {}

        for timeframe, config in self.timeframe_config.items():
            metrics = self._analyze_timeframe(
                symbol=symbol,
                timeframe=timeframe,
                correlation_lookback=int(
                    config["correlation_lookback"]
                ),
                move_lookback=int(
                    config["move_lookback"]
                ),
            )

            suffix = timeframe

            result.update({
                f"btc_corr_{suffix}": metrics["correlation"],
                f"btc_beta_{suffix}": metrics["beta"],
                f"btc_r2_{suffix}": metrics["r2"],

                f"symbol_move_{suffix}_pct": (
                    metrics["symbol_move_pct"]
                ),
                f"btc_move_{suffix}_pct": (
                    metrics["btc_move_pct"]
                ),

                f"btc_expected_move_{suffix}_pct": (
                    metrics["expected_move_pct"]
                ),

                f"btc_residual_move_{suffix}_pct": (
                    metrics["residual_move_pct"]
                ),

                f"btc_corr_available_{suffix}": (
                    metrics["available"]
                ),

                f"btc_corr_reason_{suffix}": (
                    metrics["reason"]
                ),
            })
            
        result.update(
            self._analyze_recent_btc_correlations(symbol)
        )

        return result
    
    def analyze_against_symbols(
        self,
        symbol: str,
        compared_symbols: list[str] | None,
    ) -> dict[str, Any]:
        """
        Compara el símbolo nuevo contra posiciones abiertas,
        entradas recientes u otros símbolos de referencia.

        Solo calcula y devuelve métricas.
        No bloquea operaciones.
        """

        compared_symbols = compared_symbols or []

        normalized_symbols = list(dict.fromkeys(
            compared_symbol
            for compared_symbol in compared_symbols
            if compared_symbol
            and compared_symbol != symbol
        ))

        result: dict[str, Any] = {}

        for suffix, config in self.recent_config.items():
            timeframe = str(config["timeframe"])
            lookback = int(
                config["correlation_lookback"]
            )

            correlations: list[dict[str, Any]] = []

            for compared_symbol in normalized_symbols:
                metrics = self._analyze_pair_correlation(
                    symbol_a=symbol,
                    symbol_b=compared_symbol,
                    timeframe=timeframe,
                    correlation_lookback=lookback,
                )

                if not metrics["available"]:
                    continue

                correlations.append({
                    "symbol": compared_symbol,
                    "correlation": metrics["correlation"],
                    "beta": metrics["beta"],
                    "r2": metrics["r2"],
                    "samples": metrics["samples"],
                })

            result.update(
                self._summarize_pair_correlations(
                    suffix=suffix,
                    requested_count=len(
                        normalized_symbols
                    ),
                    correlations=correlations,
                )
            )

        return result
    
    def _summarize_pair_correlations(
        self,
        suffix: str,
        requested_count: int,
        correlations: list[dict[str, Any]],
    ) -> dict[str, Any]:

        prefix = f"pair_corr_{suffix}"

        if not correlations:
            reason = (
                "no_symbols_to_compare"
                if requested_count == 0
                else "no_valid_comparisons"
            )

            return {
                f"max_{prefix}": None,
                f"avg_{prefix}": None,
                f"min_{prefix}": None,
                f"most_correlated_symbol_{suffix}": None,
                f"correlated_symbols_count_{suffix}": 0,
                f"pair_corr_compared_count_{suffix}": 0,
                f"pair_corr_requested_count_{suffix}": (
                    requested_count
                ),
                f"pair_corr_available_{suffix}": False,
                f"pair_corr_reason_{suffix}": reason,
            }

        # Para riesgo de LONG simultáneos nos interesa
        # principalmente la correlación positiva más alta.
        most_correlated = max(
            correlations,
            key=lambda item: item["correlation"],
        )

        correlation_values = [
            item["correlation"]
            for item in correlations
        ]

        correlated_count = sum(
            correlation
            >= self.pair_correlation_threshold
            for correlation in correlation_values
        )

        return {
            f"max_{prefix}": round(
                max(correlation_values),
                4,
            ),
            f"avg_{prefix}": round(
                float(np.mean(correlation_values)),
                4,
            ),
            f"min_{prefix}": round(
                min(correlation_values),
                4,
            ),
            f"most_correlated_symbol_{suffix}": (
                most_correlated["symbol"]
            ),
            f"correlated_symbols_count_{suffix}": (
                correlated_count
            ),
            f"pair_corr_compared_count_{suffix}": (
                len(correlations)
            ),
            f"pair_corr_requested_count_{suffix}": (
                requested_count
            ),
            f"pair_corr_available_{suffix}": True,
            f"pair_corr_reason_{suffix}": None,
        }
    
    def _analyze_recent_btc_correlations(
        self,
        symbol: str,
    ) -> dict[str, Any]:

        result: dict[str, Any] = {}

        for suffix, config in self.recent_config.items():
            timeframe = str(config["timeframe"])
            lookback = int(
                config["correlation_lookback"]
            )

            metrics = self._analyze_pair_correlation(
                symbol_a=symbol,
                symbol_b=self.btc_symbol,
                timeframe=timeframe,
                correlation_lookback=lookback,
            )

            result.update({
                f"btc_corr_{suffix}": (
                    metrics["correlation"]
                ),
                f"btc_beta_{suffix}": metrics["beta"],
                f"btc_r2_{suffix}": metrics["r2"],

                f"btc_corr_available_{suffix}": (
                    metrics["available"]
                ),
                f"btc_corr_reason_{suffix}": (
                    metrics["reason"]
                ),
                f"btc_corr_samples_{suffix}": (
                    metrics["samples"]
                ),
            })

        return result

    def _analyze_timeframe(
        self,
        symbol: str,
        timeframe: str,
        correlation_lookback: int,
        move_lookback: int,
    ) -> dict[str, Any]:

        symbol_df = self._load_prices(
            symbol,
            timeframe,
        )

        btc_df = self._load_prices(
            self.btc_symbol,
            timeframe,
        )

        if symbol_df.empty or btc_df.empty:
            return self._empty_result(
                "missing_market_data"
            )

        merged = symbol_df.merge(
            btc_df,
            on="timestamp",
            how="inner",
            suffixes=("_symbol", "_btc"),
        )

        required_rows = max(
            correlation_lookback + 1,
            move_lookback + 1,
        )

        if len(merged) < required_rows:
            return self._empty_result(
                f"not_enough_data:{len(merged)}/{required_rows}"
            )

        correlation_df = merged.tail(
            correlation_lookback + 1
        ).copy()

        symbol_returns = (
            correlation_df["close_symbol"]
            .pct_change()
        )

        btc_returns = (
            correlation_df["close_btc"]
            .pct_change()
        )

        returns_df = pd.DataFrame({
            "symbol": symbol_returns,
            "btc": btc_returns,
        }).dropna()

        if len(returns_df) < 10:
            return self._empty_result(
                "not_enough_return_samples"
            )

        correlation = returns_df[
            "symbol"
        ].corr(returns_df["btc"])

        btc_variance = returns_df["btc"].var()

        if (
            pd.isna(correlation)
            or pd.isna(btc_variance)
            or btc_variance <= 0
        ):
            return self._empty_result(
                "invalid_variance_or_correlation"
            )

        covariance = returns_df[
            "symbol"
        ].cov(returns_df["btc"])

        beta = float(covariance / btc_variance)
        correlation = float(correlation)
        r2 = correlation ** 2

        move_df = merged.tail(
            move_lookback + 1
        )

        symbol_start = float(
            move_df["close_symbol"].iloc[0]
        )
        symbol_end = float(
            move_df["close_symbol"].iloc[-1]
        )

        btc_start = float(
            move_df["close_btc"].iloc[0]
        )
        btc_end = float(
            move_df["close_btc"].iloc[-1]
        )

        if symbol_start <= 0 or btc_start <= 0:
            return self._empty_result(
                "invalid_start_price"
            )

        symbol_move_pct = (
            (symbol_end / symbol_start) - 1
        ) * 100

        btc_move_pct = (
            (btc_end / btc_start) - 1
        ) * 100

        expected_move_pct = (
            beta * btc_move_pct
        )

        residual_move_pct = (
            symbol_move_pct
            - expected_move_pct
        )

        return {
            "available": True,
            "reason": None,

            "correlation": round(
                correlation,
                4,
            ),

            "beta": round(
                beta,
                4,
            ),

            "r2": round(
                r2,
                4,
            ),

            "symbol_move_pct": round(
                symbol_move_pct,
                4,
            ),

            "btc_move_pct": round(
                btc_move_pct,
                4,
            ),

            "expected_move_pct": round(
                expected_move_pct,
                4,
            ),

            "residual_move_pct": round(
                residual_move_pct,
                4,
            ),
        }
        
    def _analyze_pair_correlation(
        self,
        symbol_a: str,
        symbol_b: str,
        timeframe: str,
        correlation_lookback: int,
    ) -> dict[str, Any]:

        symbol_a_df = self._load_prices(
            symbol_a,
            timeframe,
        )

        symbol_b_df = self._load_prices(
            symbol_b,
            timeframe,
        )

        if symbol_a_df.empty or symbol_b_df.empty:
            return self._empty_pair_result(
                "missing_market_data"
            )

        merged = symbol_a_df.merge(
            symbol_b_df,
            on="timestamp",
            how="inner",
            suffixes=("_a", "_b"),
        )

        required_rows = correlation_lookback + 1

        if len(merged) < required_rows:
            return self._empty_pair_result(
                f"not_enough_data:{len(merged)}/{required_rows}"
            )

        correlation_df = merged.tail(
            required_rows
        ).copy()

        returns_df = pd.DataFrame({
            "a": (
                correlation_df["close_a"]
                .pct_change()
            ),
            "b": (
                correlation_df["close_b"]
                .pct_change()
            ),
        }).dropna()

        # La ventana de 1h tiene solamente 12 muestras.
        # Exigimos al menos 10 para que sea utilizable.
        if len(returns_df) < 10:
            return self._empty_pair_result(
                f"not_enough_return_samples:{len(returns_df)}"
            )

        correlation = returns_df["a"].corr(
            returns_df["b"]
        )

        variance_b = returns_df["b"].var()

        if (
            pd.isna(correlation)
            or pd.isna(variance_b)
            or variance_b <= 0
        ):
            return self._empty_pair_result(
                "invalid_variance_or_correlation"
            )

        covariance = returns_df["a"].cov(
            returns_df["b"]
        )

        beta = float(covariance / variance_b)
        correlation = float(correlation)

        return {
            "available": True,
            "reason": None,
            "correlation": round(correlation, 4),
            "beta": round(beta, 4),
            "r2": round(correlation ** 2, 4),
            "samples": int(len(returns_df)),
        }
        
    @staticmethod
    def _empty_pair_result(
        reason: str,
    ) -> dict[str, Any]:

        return {
            "available": False,
            "reason": reason,
            "correlation": None,
            "beta": None,
            "r2": None,
            "samples": 0,
        }

    def _load_prices(
        self,
        symbol: str,
        timeframe: str,
    ) -> pd.DataFrame:

        try:
            candles = self.buffer.get_candles(
                symbol,
                timeframe,
            )
        except Exception:
            return pd.DataFrame()

        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)

        if not {
            "timestamp",
            "close",
        }.issubset(df.columns):
            return pd.DataFrame()

        result = df[
            ["timestamp", "close"]
        ].copy()

        result["timestamp"] = pd.to_numeric(
            result["timestamp"],
            errors="coerce",
        )

        result["close"] = pd.to_numeric(
            result["close"],
            errors="coerce",
        )

        return (
            result
            .dropna(subset=["timestamp", "close"])
            .drop_duplicates(
                subset=["timestamp"],
                keep="last",
            )
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

    @staticmethod
    def _empty_result(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "correlation": None,
            "beta": None,
            "r2": None,
            "symbol_move_pct": None,
            "btc_move_pct": None,
            "expected_move_pct": None,
            "residual_move_pct": None,
        }

    def _btc_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {}

        for timeframe in self.timeframe_config:
            result.update({
                f"btc_corr_{timeframe}": 1.0,
                f"btc_beta_{timeframe}": 1.0,
                f"btc_r2_{timeframe}": 1.0,

                f"symbol_move_{timeframe}_pct": None,
                f"btc_move_{timeframe}_pct": None,
                f"btc_expected_move_{timeframe}_pct": None,
                f"btc_residual_move_{timeframe}_pct": 0.0,

                f"btc_corr_available_{timeframe}": True,
                f"btc_corr_reason_{timeframe}": (
                    "symbol_is_btc"
                ),
            })

        for suffix, config in self.recent_config.items():
            result.update({
                f"btc_corr_{suffix}": 1.0,
                f"btc_beta_{suffix}": 1.0,
                f"btc_r2_{suffix}": 1.0,
                f"btc_corr_available_{suffix}": True,
                f"btc_corr_reason_{suffix}": (
                    "symbol_is_btc"
                ),
                f"btc_corr_samples_{suffix}": int(
                    config["correlation_lookback"]
                ),
            })

        return result