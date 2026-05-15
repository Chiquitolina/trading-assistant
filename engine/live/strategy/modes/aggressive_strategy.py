from enums.actions import Action
from enums.direction import Direction
from enums.momentum import Momentum

from models.trade_action import TradeAction

from config.timeframes import MODE_CONFIG

MODE = MODE_CONFIG["aggressive"]
MIN_ATR_PCT = MODE.get("min_atr_pct", 0.15)
EARLY_BREAKDOWN_MIN_ATR_PCT = 0.18
FAILED_BREAKDOWN_MIN_ATR_PCT = MIN_ATR_PCT

MIN_PLAN_SCORE = 4

class AggressiveStrategy:
    
    def score_plan(self, signal, side: str, reason: str) -> int:
        score = 0

        # =========================
        # SETUP QUALITY
        # =========================
        if reason in [
            "aggressive_bear_trap_long",
            "aggressive_bull_trap_short",
        ]:
            score += 3

        elif "swing_reversal" in reason:
            score += 2

        elif "trend" in reason:
            score += 1

        # =========================
        # EMA20 QUALITY
        # =========================
        dist_ema20_pct = abs(
            (signal.signal_price - signal.ema20_1m)
            / signal.ema20_1m
            * 100
        )

        if dist_ema20_pct <= 0.04:
            score += 3
        elif dist_ema20_pct <= 0.07:
            score += 2
        elif dist_ema20_pct <= 0.10:
            score += 1
        else:
            score -= 2

        # =========================
        # ATR QUALITY
        # =========================
        if signal.atr_5m_pct >= 0.30:
            score += 2
        elif signal.atr_5m_pct >= 0.20:
            score += 1

        # =========================
        # MICRO QUALITY
        # =========================
        if side == "LONG" and signal.micro == Momentum.BULLISH_PRESSURE:
            score += 2
        elif side == "LONG" and signal.micro in [
            Momentum.INSIDE_BULLISH_WEAK,
            Momentum.TREND_CONTINUATION_UP,
        ]:
            score += 1

        if side == "SHORT" and signal.micro == Momentum.BEARISH_PRESSURE:
            score += 2
        elif side == "SHORT" and signal.micro in [
            Momentum.INSIDE_BEARISH_WEAK,
            Momentum.TREND_CONTINUATION_DOWN,
        ]:
            score += 1

        # =========================
        # HTF EXTENSION PENALTY
        # =========================
        if signal.dist_ema50_4h_pct is not None:
            htf_ext = abs(signal.dist_ema50_4h_pct)

            if htf_ext > 35:
                score -= 3
            elif htf_ext > 20:
                score -= 2
            elif htf_ext > 12:
                score -= 1

        return score

    def evaluate(
        self,
        signal,
        previous_direction=None,
        current_position=None,
    ):

        if current_position:
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="position_already_open",
            )

        # =========================
        # OVEREXTENDED FILTER
        # =========================

        dist_ema20_pct = abs(
            (signal.signal_price - signal.ema20_1m)
            / signal.ema20_1m
            * 100
        )

        overextended = dist_ema20_pct > 0.10

        # =========================
        # LATE CONTINUATION FILTERS
        # =========================

        weak_late_short = (
            signal.near_swing_low
            and signal.momentum in [
                Momentum.BEARISH_PRESSURE,
                Momentum.INSIDE_BEARISH_WEAK,
            ]
        )

        weak_late_long = (
            signal.near_swing_high
            and signal.momentum in [
                Momentum.BULLISH_PRESSURE,
                Momentum.INSIDE_BULLISH_WEAK,
            ]
        )

        # =========================
        # BAD SHORT SEQUENCE FILTER
        # =========================

        bad_short_after_bullish_pressure = (
            signal.momentum == Momentum.BEARISH_PRESSURE
            and signal.momentum_prev1 == Momentum.BULLISH_PRESSURE
        )
        
                # =========================
        # HTF OVEREXTENSION FILTER
        # =========================

        htf_overextended_long = (
            (
                signal.dist_ema50_15m_pct is not None
                and signal.dist_ema50_15m_pct > 4
            )
            or (
                signal.dist_ema50_1h_pct is not None
                and signal.dist_ema50_1h_pct > 6
            )
            or (
                signal.dist_ema50_4h_pct is not None
                and signal.dist_ema50_4h_pct > 10
            )
        )

        # =========================
        # FAILED BREAKDOWN LONG
        # =========================

        failed_breakdown_long = (
            signal.near_swing_low
            and signal.near_ema20_long
            and signal.near_ema50_long
            and signal.direction == Direction.DOWN
            and signal.momentum == Momentum.BULLISH_PRESSURE
            and signal.momentum_prev1 in [
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.INDECISION,
            ]
            and signal.atr_5m_pct >= FAILED_BREAKDOWN_MIN_ATR_PCT
            and signal.signal_price < signal.ema100_5m
        )

        # =========================
        # BEAR TRAP LONG
        # =========================

        bear_trap_long = (
            signal.htf_bullish
            and signal.direction != Direction.DOWN

            and signal.near_swing_low
            and signal.momentum == Momentum.EXHAUSTION_DOWN

            and signal.momentum_prev1 in [
                Momentum.BEARISH_PRESSURE,
                Momentum.BREAKOUT_DOWN_WEAK,
                Momentum.BREAKOUT_DOWN_STRONG,
                Momentum.TREND_CONTINUATION_DOWN,
            ]

            and signal.micro in [
                Momentum.BULLISH_PRESSURE,
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.TREND_CONTINUATION_UP,
            ]

            and signal.atr_5m_pct >= MIN_ATR_PCT
        )

        # =========================
        # BULL TRAP SHORT
        # =========================

        bull_trap_short = (
            signal.htf_bearish
            and signal.direction != Direction.UP

            and signal.near_swing_high
            and signal.momentum == Momentum.EXHAUSTION_UP

            and signal.momentum_prev1 in [
                Momentum.BULLISH_PRESSURE,
                Momentum.BREAKOUT_UP_WEAK,
                Momentum.BREAKOUT_UP_STRONG,
                Momentum.TREND_CONTINUATION_UP,
            ]

            and signal.micro in [
                Momentum.BEARISH_PRESSURE,
                Momentum.INSIDE_BEARISH_WEAK,
                Momentum.TREND_CONTINUATION_DOWN,
            ]

            and signal.atr_5m_pct >= MIN_ATR_PCT
        )

        # =========================
        # GENERIC REVERSALS
        # =========================

        long_signal = (
            signal.momentum == Momentum.EXHAUSTION_DOWN
            and signal.near_swing_low
            and signal.htf_bullish
            and signal.direction != Direction.DOWN
        )

        short_signal = (
            signal.momentum == Momentum.EXHAUSTION_UP
            and signal.near_swing_high
            and signal.htf_bearish
            and signal.direction != Direction.UP
        )

        # =========================
        # TREND CONTINUATIONS
        # =========================

        trend_long_signal_raw = (
            signal.htf_bullish
            and signal.direction == Direction.UP
            and signal.near_ema20_long
            and signal.momentum in [
                Momentum.BULLISH_PRESSURE,
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.TREND_CONTINUATION_UP,
            ]
        )

        trend_short_signal_raw = (
            signal.htf_bearish
            and signal.direction == Direction.DOWN
            and signal.near_ema20_short
            and signal.momentum in [
                Momentum.BEARISH_PRESSURE,
                Momentum.INSIDE_BEARISH_WEAK,
                Momentum.TREND_CONTINUATION_DOWN,
            ]
        )

        # =========================
        # EARLY BREAKDOWN
        # =========================

        early_breakdown_short_raw = (
            signal.htf_bearish
            and signal.direction == Direction.DOWN
            and signal.signal_price < signal.ema20_1m
            and signal.ema20_1m < signal.ema34_1m
            and signal.momentum in [
                Momentum.BEARISH_PRESSURE,
                Momentum.TREND_CONTINUATION_DOWN,
            ]
        )

        # =========================
        # EMA50 PULLBACK LONG
        # =========================

        trend_long_ema50 = (
            signal.htf_bullish
            and signal.direction == Direction.UP
            and signal.ema_alignment_bullish
            and signal.near_ema50_long
            and signal.signal_price > signal.ema50_1m
            and signal.momentum in [
                Momentum.EXHAUSTION_DOWN,
                Momentum.INSIDE_BULLISH_WEAK,
                Momentum.BULLISH_PRESSURE,
            ]
        )

        # =========================
        # FINAL FILTERS
        # =========================

        trend_long_signal = (
            trend_long_signal_raw
            and not overextended
            and not weak_late_long
        )

        trend_short_signal = (
            trend_short_signal_raw
            and not overextended
            and not weak_late_short
            and not bad_short_after_bullish_pressure
        )

        early_breakdown_short = (
            early_breakdown_short_raw
            and not overextended
            and not weak_late_short
            and not bad_short_after_bullish_pressure
            and signal.atr_5m_pct >= EARLY_BREAKDOWN_MIN_ATR_PCT
        )

        # =========================
        # BLOCK REASONS
        # =========================

        if (
            overextended
            and (
                trend_long_signal_raw
                or trend_short_signal_raw
                or early_breakdown_short_raw
            )
        ):
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason=f"blocked_overextended_ema20_{dist_ema20_pct:.3f}pct",
            )

        if weak_late_short and (
            trend_short_signal_raw
            or early_breakdown_short_raw
        ):
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="blocked_weak_late_short_near_swing_low",
            )

        if weak_late_long and trend_long_signal_raw:
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="blocked_weak_late_long_near_swing_high",
            )

        if bad_short_after_bullish_pressure and (
            trend_short_signal_raw
            or early_breakdown_short_raw
        ):
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="blocked_short_after_bullish_pressure",
            )
            
        if htf_overextended_long and (
            long_signal
            or trend_long_ema50
            or trend_long_signal
        ):
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason=(
                    "blocked_long_htf_overextended_"
                    f"15m_{signal.dist_ema50_15m_pct}_"
                    f"1h_{signal.dist_ema50_1h_pct}_"
                    f"4h_{signal.dist_ema50_4h_pct}"
                ),
            )

        if (
            early_breakdown_short_raw
            and signal.atr_5m_pct < EARLY_BREAKDOWN_MIN_ATR_PCT
        ):
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason=f"blocked_early_breakdown_low_atr_pct_{signal.atr_5m_pct:.3f}",
            )

        # =========================
        # EXECUTION
        # =========================

        if failed_breakdown_long:
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_failed_breakdown_long",
            )
            
        if bear_trap_long:
            score = self.score_plan(
                signal=signal,
                side="LONG",
                reason="aggressive_bear_trap_long",
            )

            if score < MIN_PLAN_SCORE:
                return TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="aggressive_strategy",
                    reason=f"blocked_low_score_{score}_aggressive_bear_trap_long",
                )

            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason=f"aggressive_bear_trap_long_score_{score}",
            )

        if bull_trap_short:
            score = self.score_plan(
                signal=signal,
                side="SHORT",
                reason="aggressive_bull_trap_short",
            )

            if score < MIN_PLAN_SCORE:
                return TradeAction(
                    action=Action.HOLD,
                    signal=signal,
                    strategy_name="aggressive_strategy",
                    reason=f"blocked_low_score_{score}_aggressive_bull_trap_short",
                )

            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason=f"aggressive_bull_trap_short_score_{score}",
            )

        if long_signal:
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_long_swing_reversal",
            )

        if trend_long_signal:
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_trend_long",
            )

        if trend_long_ema50:
            return TradeAction(
                action=Action.LONG,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_ema50_pullback",
            )

        if short_signal:
            return TradeAction(
                action=Action.HOLD,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="blocked_generic_short_swing_reversal_disabled",
            )

        if trend_short_signal:
            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_trend_short",
            )

        if early_breakdown_short:
            return TradeAction(
                action=Action.SHORT,
                signal=signal,
                strategy_name="aggressive_strategy",
                reason="aggressive_early_breakdown_short",
            )

        return TradeAction(
            action=Action.HOLD,
            signal=signal,
            strategy_name="aggressive_strategy",
            reason="no_aggressive_setup",
        )