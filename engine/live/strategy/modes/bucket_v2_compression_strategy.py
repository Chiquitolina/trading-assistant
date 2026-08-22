import pandas as pd

from enums.actions import Action
from models.compression_result import CompressionResult
from models.execution_variant import ExecutionVariant
from models.trade_action import TradeAction
from signals.indicators.atr import add_atr
from signals.indicators.bucket_v2_compression_state_machine import BucketV2CompressionStateMachine
from signals.indicators.compression_breakout_detector import (
    detect_breakout_from_watch, detect_compression_breakout,
)
from signals.indicators.compression_detector import detect_compression
from signals.indicators.trend_detector import detect_trend_up


class BucketV2CompressionStrategy:
    """Independent strategy implementation characterized from ``new-bucket``."""

    def __init__(self, buffer, journal=None, max_watch_candles=8,
                 max_pullback_candles=5, pullback_max_pct=1.2,
                 pullback_min_hold_high=True, **_ignored):
        self.stats = {}
        self.buffer = buffer
        self.journal = journal
        self.machine = BucketV2CompressionStateMachine(
            max_watch_candles, max_pullback_candles,
            pullback_max_pct, pullback_min_hold_high,
        )
        self.last_context_by_symbol = {}

    @staticmethod
    def _build_signal_context(signal, compression_state, trend, compression,
                              breakout, btc_context=None):
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
            "compression_range_pct": compression_state.get("compression_range_pct"),
            "range_ratio": compression_state.get("range_ratio"),
            "atr_ratio": compression_state.get("atr_ratio"),
            "volume_ratio": compression_state.get("volume_ratio"),
            "avg_body_pct": compression_state.get("avg_body_pct"),
            "compression_height_pct": compression_state.get("compression_height_pct"),
            "compression_duration": compression_state.get("compression_duration"),
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

    def evaluate(self, symbol, signal, tf="15m", btc_context=None,
                 current_position=None):
        if signal is None:
            return CompressionResult(
                TradeAction(Action.HOLD, None, "compression_strategy", "no_signal"), {}
            )
        candles = self.buffer.get_candles(symbol, tf)
        if len(candles) < 80:
            self._count_state("SKIPPED_NOT_ENOUGH_CANDLES")
            return CompressionResult(
                TradeAction(Action.HOLD, signal, "compression_strategy",
                            "compression_not_enough_candles"), {}
            )
        df_tf = add_atr(pd.DataFrame(candles))
        prev_df = df_tf.iloc[:-1].copy()
        atr = float(df_tf["atr"].iloc[-1])
        trend = detect_trend_up(prev_df, lookback=20, ema_fast=20,
                                ema_slow=50, min_score=4)
        compression = detect_compression(
            prev_df, lookback=10, base_lookback=40, max_range_ratio=.65,
            max_atr_ratio=.75, max_volume_ratio=.95, max_body_pct=.50,
            min_score=3,
        )
        watch = self.machine.get(symbol)
        breakout = (
            detect_breakout_from_watch(df_tf, watch.compression_high, 20, 1.5)
            if watch is not None else detect_compression_breakout(df_tf)
        )
        state = self.machine.update(
            symbol, df_tf.iloc[-1].to_dict(), trend, compression, breakout, atr,
        )
        self._count_state(state["state"])
        self.last_context_by_symbol[symbol] = {
            "trend": trend, "compression": compression,
            "breakout": breakout, "compression_state": state,
        }
        self._log(symbol, state, trend, compression, breakout, df_tf, prev_df)
        context = self._build_signal_context(
            signal, state, trend, compression, breakout, btc_context,
        )
        setup_id = f"{symbol}:{state.get('created_ts')}:{state.get('breakout_ts')}"
        if state["state"] == "ENTRY_READY":
            action = TradeAction(
                Action.LONG, signal, "compression_strategy", "compression_breakout",
                ExecutionVariant.BUCKET_V2, setup_id, "compression_breakout",
            )
        else:
            action = TradeAction(
                Action.HOLD, signal, "compression_strategy",
                state.get("reason", "compression_waiting"),
            )
        return CompressionResult(action, context)

    def _log(self, symbol, state, trend, compression, breakout, df_tf, prev_df):
        if not self.journal or state["state"] == "IDLE":
            return
        # Keep the audit payload from new-bucket intact.  The independent V2
        # pipeline must be comparable with historical watch journals.
        self.journal.log(symbol=symbol, event=state["state"], data={
            "reason": state.get("reason"),
            "watch_age": state.get("watch_age"),
            "candles_waiting": state.get("candles_waiting"),
            "compression_created_ts": state.get("created_ts"),
            "compression_updated_ts": state.get("updated_ts"),
            "compression_high": state.get("compression_high"),
            "compression_low": state.get("compression_low"),
            "compression_score": state.get("compression_score"),
            "trend_score": state.get("trend_score"),
            "compression_height_pct": state.get("compression_height_pct"),
            "compression_duration": state.get("compression_duration"),
            "upper_slope": state.get("upper_slope"),
            "lower_slope": state.get("lower_slope"),
            "slope_difference": state.get("slope_difference"),
            "touches_high": state.get("touches_high"),
            "touches_low": state.get("touches_low"),
            "touches_high_ratio": state.get("touches_high_ratio"),
            "touches_low_ratio": state.get("touches_low_ratio"),
            "touch_imbalance": state.get("touch_imbalance"),
            "touch_imbalance_ratio": state.get("touch_imbalance_ratio"),
            "inside_ratio": state.get("inside_ratio"),
            "compression_shape": state.get("compression_shape"),
            "compression_quality_label": state.get("compression_quality_label"),
            "compression_range_pct": state.get("compression_range_pct"),
            "range_ratio": state.get("range_ratio"),
            "atr_ratio": state.get("atr_ratio"),
            "volume_ratio": state.get("volume_ratio"),
            "avg_body_pct": state.get("avg_body_pct"),
            "compression_reasons": compression.get("reasons"),
            "breakout_detected": breakout.get("breakout"),
            "breakout_reason": breakout.get("reason"),
            "breakout_failed_reasons": breakout.get("failed_reasons"),
            "breakout_volume_ratio": breakout.get("volume_ratio"),
            "breakout_price": state.get("breakout_price"),
            "breakout_high": state.get("breakout_high"),
            "breakout_extension_pct": state.get("breakout_extension_pct"),
            "breakout_extension_atr": state.get("breakout_extension_atr"),
            "pullback_pct": state.get("pullback_pct"),
            "valid_pullback": state.get("valid_pullback"),
            "holds_compression_high": state.get("holds_compression_high"),
            "continuation": state.get("continuation"),
            "last_candle": df_tf.iloc[-1].to_dict(),
            "last_10_candles": prev_df[
                ["open", "high", "low", "close", "volume"]
            ].tail(10).to_dict("records"),
        })

    def active_watches(self): return self.machine.active_watches()
    def alive_watches_count(self): return len(self.machine.watches)
    def reset_stats(self): self.stats = {}
    def get_stats(self): return dict(self.stats)
    def _count_state(self, state): self.stats[state] = self.stats.get(state, 0) + 1
