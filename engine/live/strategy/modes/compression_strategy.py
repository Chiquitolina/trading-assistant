import pandas as pd

from enums.actions import Action
from models.trade_action import TradeAction
from models.compression_result import CompressionResult

from signals.indicators.atr import add_atr
from signals.indicators.trend_detector import detect_trend_up
from signals.indicators.compression_detector import detect_compression
from signals.indicators.compression_breakout_detector import (
    detect_compression_breakout,
    detect_breakout_from_watch,
)
from signals.indicators.compression_state_machine import CompressionStateMachine


class CompressionStrategy:

    COMPRESSION_LOOKBACK = 20
    COMPRESSION_BASE_LOOKBACK = 80
    COMPRESSION_BASE_MODE = "separate_fixed"
    MIN_REQUIRED_CANDLES = (
        COMPRESSION_LOOKBACK
        + COMPRESSION_BASE_LOOKBACK
        + 20
        + 1
    )

    def __init__(
        self,
        buffer,
        journal=None,
        max_watch_candles=8,
        max_pullback_candles=5,
        pullback_max_pct=1.2,
        pullback_min_hold_high=True,
    ):
        self.stats = {}
        self.buffer = buffer
        self.journal = journal

        self.machine = CompressionStateMachine(
            max_watch_candles=max_watch_candles,
            max_pullback_candles=max_pullback_candles,
            pullback_max_pct=pullback_max_pct,
            pullback_min_hold_high=pullback_min_hold_high,
        )

        self.last_context_by_symbol = {}
        
    def _build_signal_context(
        self,
        signal,
        compression_state,
        trend,
        compression,
        breakout,
        btc_context=None,
    ):
        return {
            "trend": signal.trend.value,
            "direction": signal.direction.value,
            "momentum": signal.momentum.value,

            "strategy_name": "compression",

            "compression_state": compression_state.get("state"),
            "compression_reason": compression_state.get("reason"),

            "compression_created_ts": compression_state.get("created_ts"),
            "compression_updated_ts": compression_state.get("updated_ts"),
            "compression_candles_waiting": compression_state.get("candles_waiting"),

            "trend_score": compression_state.get("trend_score") or trend.get("score"),
            "trend_reasons": trend.get("reasons"),
            "compression_trend_up": trend.get("trend_up"),

            "compression_score": compression_state.get("compression_score") or compression.get("score"),
            "compression_reasons": compression.get("reasons"),
            "compression_is_compression": compression.get("is_compression"),

            "compression_high": compression_state.get("compression_high"),
            "compression_low": compression_state.get("compression_low"),
            "compression_range_pct": compression_state.get(
                "compression_range_pct"
            ),

            "range_ratio": compression_state.get(
                "range_ratio"
            ),
            "atr_ratio": compression_state.get(
                "atr_ratio"
            ),
            "volume_ratio": compression_state.get(
                "volume_ratio"
            ),
            "avg_body_pct": compression_state.get(
                "avg_body_pct"
            ),
            
            "compression_height_pct": compression_state.get("compression_height_pct"),
            "compression_duration": compression_state.get("compression_duration"),

            "compression_selected_lookback": self.COMPRESSION_LOOKBACK,
            "compression_base_lookback": self.COMPRESSION_BASE_LOOKBACK,
            "compression_base_mode": self.COMPRESSION_BASE_MODE,

            "upper_slope": compression_state.get("upper_slope"),
            "lower_slope": compression_state.get("lower_slope"),
            "slope_difference": compression_state.get("slope_difference"),

            "touches_high": compression_state.get("touches_high"),
            "touches_low": compression_state.get("touches_low"),

            "touches_high_ratio": compression_state.get("touches_high_ratio"),
            "touches_low_ratio": compression_state.get("touches_low_ratio"),

            "touch_imbalance": compression_state.get("touch_imbalance"),
            "touch_imbalance_ratio": compression_state.get("touch_imbalance_ratio"),

            "inside_ratio": compression_state.get("inside_ratio"),

            "compression_shape": compression_state.get("compression_shape"),
            "compression_quality_label": compression_state.get("compression_quality_label"),

            "breakout_detected": breakout.get("breakout"),
            "breakout_ts": compression_state.get("breakout_ts"),
            "breakout_price": compression_state.get("breakout_price"),
            "breakout_high": compression_state.get("breakout_high"),
            "breakout_volume_ratio": compression_state.get("breakout_volume_ratio"),

            "breakout_extension_pct": compression_state.get("breakout_extension_pct"),
            "breakout_extension_atr": compression_state.get("breakout_extension_atr"),

            "entry_ready_price": compression_state.get("entry_price"),
            
            "btc_velocity_15m": getattr(btc_context, "velocity_15m", None),
            "btc_velocity_1h": getattr(btc_context, "velocity_1h", None),
            "btc_direction_15m": getattr(btc_context, "direction_15m", None),
            "btc_direction_1h": getattr(btc_context, "direction_1h", None),
            "btc_context_state": getattr(btc_context, "state", None),
            "btc_context_reason": getattr(btc_context, "reason", None),
        }

    def evaluate(
        self,
        symbol,
        signal,
        tf="30m",
        btc_context=None,
        current_position=None,
    ):

        if signal is None:
            trade_action = TradeAction(
                action=Action.HOLD,
                signal=None,
                strategy_name="compression_strategy",
                reason="no_signal"
            )

            return CompressionResult(
                trade_action=trade_action,
                signal_context={}
            )

        candles = self.buffer.get_candles(symbol, tf)

        if len(candles) < self.MIN_REQUIRED_CANDLES:
            trade_action = TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="compression_strategy",
                reason="compression_not_enough_candles"
            )

            self._count_state("SKIPPED_NOT_ENOUGH_CANDLES")

            return CompressionResult(
                trade_action=trade_action,
                signal_context={}
            )

        df_tf = pd.DataFrame(candles)
        df_tf = add_atr(df_tf)

        prev_df = df_tf.iloc[:-1].copy()
        atr = float(df_tf["atr"].iloc[-1])

        trend = detect_trend_up(
            prev_df,
            lookback=20,
            ema_fast=20,
            ema_slow=50,
            min_score=4,
        )

        compression = detect_compression(
            prev_df,
            lookback=self.COMPRESSION_LOOKBACK,
            base_lookback=self.COMPRESSION_BASE_LOOKBACK,
            max_range_ratio=0.65,
            max_atr_ratio=0.75,
            max_volume_ratio=0.95,
            max_body_pct=0.50,
            min_score=3,
        )

        watch = self.machine.get(symbol)

        if watch is not None:
            breakout = detect_breakout_from_watch(
                df_tf,
                compression_high=watch.compression_high,
                volume_lookback=20,
                min_volume_expansion=1.5,
            )
        else:
            breakout = detect_compression_breakout(df_tf)

        compression_state = self.machine.update(
            symbol=symbol,
            candle=df_tf.iloc[-1].to_dict(),
            trend=trend,
            compression=compression,
            breakout=breakout,
            atr=atr,
        )
        
        self._count_state(compression_state["state"])

        self.last_context_by_symbol[symbol] = {
            "trend": trend,
            "compression": compression,
            "breakout": breakout,
            "compression_state": compression_state,
        }

        self._log(
            symbol=symbol,
            compression_state=compression_state,
            trend=trend,
            compression=compression,
            breakout=breakout,
            df_tf=df_tf,
            prev_df=prev_df,
        )

        if compression_state["state"] != "IDLE":
            print(
                f"\033[96m[COMPRESSION]\033[0m "
                f"symbol={symbol} "
                f"state={compression_state['state']} "
                f"reason={compression_state.get('reason')}"
            )

        signal_context = self._build_signal_context(
            signal=signal,
            compression_state=compression_state,
            trend=trend,
            compression=compression,
            breakout=breakout,
            btc_context=btc_context
        )

        if compression_state["state"] == "ENTRY_READY":
            trade_action = TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="compression_strategy",
                reason="compression_breakout"
            )
        else:
            trade_action = TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="compression_strategy",
                reason=compression_state.get("reason", "compression_waiting")
            )

        return CompressionResult(
            trade_action=trade_action,
            signal_context=signal_context,
        )

    def get_context(self, symbol):
        return self.last_context_by_symbol.get(symbol, {})

    def active_watches(self):
        return self.machine.active_watches()

    def alive_watches_count(self):
        return len(self.machine.watches)
    
    def reset_stats(self):
        self.stats = {}

    def get_stats(self):
        return dict(self.stats)

    def _count_state(self, state):
        self.stats[state] = self.stats.get(state, 0) + 1

    def _log(
        self,
        symbol,
        compression_state,
        trend,
        compression,
        breakout,
        df_tf,
        prev_df,
    ):
        if not self.journal:
            return

        if compression_state["state"] == "IDLE":
            return

        self.journal.log(
            symbol=symbol,
            event=compression_state["state"],
            data={
                "reason": compression_state.get("reason"),
                "watch_age": compression_state.get("watch_age"),
                "candles_waiting": compression_state.get("candles_waiting"),
                
                "compression_created_ts": compression_state.get("created_ts"),
                "compression_updated_ts": compression_state.get("updated_ts"),

                "compression_high": compression_state.get("compression_high"),
                "compression_low": compression_state.get("compression_low"),
                "compression_score": compression_state.get("compression_score"),
                "trend_score": compression_state.get("trend_score"),

                "compression_height_pct": compression_state.get("compression_height_pct"),
                "compression_duration": compression_state.get("compression_duration"),

                "compression_selected_lookback": self.COMPRESSION_LOOKBACK,
                "compression_base_lookback": self.COMPRESSION_BASE_LOOKBACK,
                "compression_base_mode": self.COMPRESSION_BASE_MODE,

                "upper_slope": compression_state.get("upper_slope"),
                "lower_slope": compression_state.get("lower_slope"),
                "slope_difference": compression_state.get("slope_difference"),

                "touches_high": compression_state.get("touches_high"),
                "touches_low": compression_state.get("touches_low"),

                "touches_high_ratio": compression_state.get("touches_high_ratio"),
                "touches_low_ratio": compression_state.get("touches_low_ratio"),

                "touch_imbalance": compression_state.get("touch_imbalance"),
                "touch_imbalance_ratio": compression_state.get("touch_imbalance_ratio"),

                "inside_ratio": compression_state.get("inside_ratio"),

                "compression_shape": compression_state.get("compression_shape"),
                "compression_quality_label": compression_state.get("compression_quality_label"),

                "compression_range_pct": compression_state.get(
                    "compression_range_pct"
                ),
                "range_ratio": compression_state.get(
                    "range_ratio"
                ),
                "atr_ratio": compression_state.get(
                    "atr_ratio"
                ),
                "volume_ratio": compression_state.get(
                    "volume_ratio"
                ),
                "avg_body_pct": compression_state.get(
                    "avg_body_pct"
                ),
                "compression_reasons": compression.get("reasons"),

                "breakout_detected": breakout.get("breakout"),
                "breakout_reason": breakout.get("reason"),
                "breakout_failed_reasons": breakout.get("failed_reasons"),
                "breakout_volume_ratio": breakout.get("volume_ratio"),

                "breakout_price": compression_state.get("breakout_price"),
                "breakout_high": compression_state.get("breakout_high"),
                "breakout_extension_pct": compression_state.get("breakout_extension_pct"),
                "breakout_extension_atr": compression_state.get("breakout_extension_atr"),

                "pullback_pct": compression_state.get("pullback_pct"),
                "valid_pullback": compression_state.get("valid_pullback"),
                "holds_compression_high": compression_state.get("holds_compression_high"),
                "continuation": compression_state.get("continuation"),

                "last_candle": df_tf.iloc[-1].to_dict(),
                "last_10_candles": prev_df[
                    ["open", "high", "low", "close", "volume"]
                ].tail(10).to_dict("records"),
            }
        )

