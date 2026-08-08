import json

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


COMPRESSION_LOOKBACKS = (10, 15, 20, 25, 30)
COMPRESSION_BASE_MULTIPLIER = 4
COMPRESSION_BASE_MODE = "separate_dynamic"
INDICATOR_WARMUP_CANDLES = 20

ALLOWED_COMPRESSION_DURATIONS = (20,)

BLOCKED_BREAKOUT_EXTENSION_MIN_PCT = 0.25
BLOCKED_BREAKOUT_EXTENSION_MAX_PCT = 0.50

class CompressionStrategy:

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

    @staticmethod
    def _selection_score(candidate):
        quality_bonus = {
            "GOOD_SHAPE": 1.0,
            "OK_SHAPE": 0.5,
            "BAD_SHAPE": 0.0,
        }.get(
            candidate.get("compression_quality_label"),
            0.0,
        )

        return round(
            float(candidate.get("score", 0))
            + quality_bonus
            + float(candidate.get("inside_ratio", 0) or 0)
            + float(candidate.get("touches_high_ratio", 0) or 0)
            + float(candidate.get("touches_low_ratio", 0) or 0),
            4,
        )

    def _detect_best_compression(self, prev_df):
        candidates = []

        for lookback in COMPRESSION_LOOKBACKS:
            base_lookback = (
                lookback
                * COMPRESSION_BASE_MULTIPLIER
            )

            candidate = detect_compression(
                prev_df,
                lookback=lookback,
                base_lookback=base_lookback,
                base_mode=COMPRESSION_BASE_MODE,
                max_range_ratio=0.65,
                max_atr_ratio=0.75,
                max_volume_ratio=0.95,
                max_body_pct=0.50,
                min_score=3,
            )

            candidate["selection_score"] = (
                self._selection_score(candidate)
            )
            candidates.append(candidate)

        valid_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("is_compression")
        ]

        candidate_pool = (
            valid_candidates
            if valid_candidates
            else candidates
        )

        selected = max(
            candidate_pool,
            key=lambda candidate: (
                float(
                    candidate.get(
                        "selection_score",
                        0,
                    )
                ),
                -int(candidate.get("lookback", 0)),
            ),
        ).copy()

        selected["selected_lookback"] = (
            selected.get("lookback")
        )
        selected["selection_reason"] = (
            "best_valid_candidate"
            if valid_candidates
            else "best_candidate_no_valid_compression"
        )
        selected["candidate_count"] = len(candidates)
        selected["valid_candidate_count"] = len(
            valid_candidates
        )

        candidate_rows = []

        for candidate in candidates:
            candidate_rows.append({
                "lookback": candidate.get("lookback"),
                "base_lookback": candidate.get(
                    "base_lookback"
                ),
                "base_mode": candidate.get("base_mode"),
                "is_compression": candidate.get(
                    "is_compression"
                ),
                "compression_score": candidate.get("score"),
                "selection_score": candidate.get(
                    "selection_score"
                ),
                "range_ratio": candidate.get("range_ratio"),
                "atr_ratio": candidate.get("atr_ratio"),
                "volume_ratio": candidate.get("volume_ratio"),
                "compression_range_pct": candidate.get(
                    "compression_range_pct"
                ),
                "compression_shape": candidate.get(
                    "compression_shape"
                ),
                "compression_quality_label": candidate.get(
                    "compression_quality_label"
                ),
                "touches_high_ratio": candidate.get(
                    "touches_high_ratio"
                ),
                "touches_low_ratio": candidate.get(
                    "touches_low_ratio"
                ),
                "touch_imbalance_ratio": candidate.get(
                    "touch_imbalance_ratio"
                ),
            })

        selected["candidates_json"] = json.dumps(
            candidate_rows,
            separators=(",", ":"),
        )

        return selected
        
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

            "compression_selected_lookback": compression_state.get("selected_lookback"),
            "compression_selection_score": compression_state.get("selection_score"),
            "compression_selection_reason": compression_state.get("selection_reason"),
            "compression_candidate_count": compression_state.get("candidate_count"),
            "compression_valid_candidate_count": compression_state.get("valid_candidate_count"),
            "compression_candidates_json": compression_state.get("compression_candidates_json"),
            "compression_base_mode": compression_state.get("base_mode"),
            "compression_base_lookback": compression_state.get("base_lookback"),

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
        tf="15m",
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

        minimum_candles = (
            max(COMPRESSION_LOOKBACKS)
            + (
                max(COMPRESSION_LOOKBACKS)
                * COMPRESSION_BASE_MULTIPLIER
            )
            + INDICATOR_WARMUP_CANDLES
            + 1
        )

        if len(candles) < minimum_candles:
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

        compression = self._detect_best_compression(
            prev_df
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

        compression_duration = compression_state.get(
            "compression_duration"
        )

        breakout_extension_pct = compression_state.get(
            "breakout_extension_pct"
        )

        try:
            breakout_extension_pct = float(
                breakout_extension_pct
            )
        except (TypeError, ValueError):
            breakout_extension_pct = None


        if compression_state["state"] == "ENTRY_READY":

            duration_allowed = (
                compression_duration
                in ALLOWED_COMPRESSION_DURATIONS
            )

            breakout_extension_blocked = (
                breakout_extension_pct is not None
                and
                BLOCKED_BREAKOUT_EXTENSION_MIN_PCT
                <= breakout_extension_pct
                <
                BLOCKED_BREAKOUT_EXTENSION_MAX_PCT
            )

            if not duration_allowed:
                print(
                    f"[COMPRESSION FILTER] "
                    f"symbol={symbol} "
                    f"duration={compression_duration} "
                    f"breakout_extension_pct="
                    f"{breakout_extension_pct} "
                    f"decision=BLOCKED "
                    f"reason=duration_not_allowed"
                )

                trade_action = TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="compression_strategy",
                    reason="compression_duration_not_allowed",
                )

            elif breakout_extension_pct is None:
                print(
                    f"[COMPRESSION FILTER] "
                    f"symbol={symbol} "
                    f"duration={compression_duration} "
                    f"breakout_extension_pct=None "
                    f"decision=BLOCKED "
                    f"reason=missing_breakout_extension_pct"
                )

                trade_action = TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="compression_strategy",
                    reason="missing_breakout_extension_pct",
                )

            elif breakout_extension_blocked:
                print(
                    f"[COMPRESSION FILTER] "
                    f"symbol={symbol} "
                    f"duration={compression_duration} "
                    f"breakout_extension_pct="
                    f"{breakout_extension_pct:.4f} "
                    f"blocked_range=["
                    f"{BLOCKED_BREAKOUT_EXTENSION_MIN_PCT}, "
                    f"{BLOCKED_BREAKOUT_EXTENSION_MAX_PCT}) "
                    f"decision=BLOCKED"
                )

                trade_action = TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="compression_strategy",
                    reason=(
                        "breakout_extension_pct_"
                        "blocked_025_050"
                    ),
                )

            else:
                print(
                    f"[COMPRESSION FILTER] "
                    f"symbol={symbol} "
                    f"duration={compression_duration} "
                    f"breakout_extension_pct="
                    f"{breakout_extension_pct:.4f} "
                    f"decision=ALLOWED"
                )

                trade_action = TradeAction(
                    action=Action.LONG,
                    signal=signal,
                    strategy_name="compression_strategy",
                    reason=(
                        "compression_breakout_"
                        "duration_and_extension_allowed"
                    ),
                )

        else:
            trade_action = TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="compression_strategy",
                reason=compression_state.get(
                    "reason",
                    "compression_waiting",
                ),
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

                "selected_lookback": compression_state.get("selected_lookback"),
                "selection_score": compression_state.get("selection_score"),
                "selection_reason": compression_state.get("selection_reason"),
                "candidate_count": compression_state.get("candidate_count"),
                "valid_candidate_count": compression_state.get("valid_candidate_count"),
                "compression_candidates_json": compression_state.get("compression_candidates_json"),
                "base_mode": compression_state.get("base_mode"),
                "base_lookback": compression_state.get("base_lookback"),

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

