from collections import deque


class RegimeDetector:
    def __init__(
        self,
        max_states: int = 24,
        min_states: int = 8,
        min_shifts: int = 2,
        fail_rate_threshold: float = 0.50,
        ema_memory_threshold: float = 0.52,
        structure_threshold: float = 0.47,
        regime_confirm_bars: int = 2,
    ):
        self.states = deque(maxlen=max_states)

        self.min_states = min_states
        self.min_shifts = min_shifts
        self.fail_rate_threshold = fail_rate_threshold
        self.ema_memory_threshold = ema_memory_threshold
        self.structure_threshold = structure_threshold
        self.regime_confirm_bars = regime_confirm_bars

        self.current_regime = "UNKNOWN"
        self.pending_regime = None
        self.pending_count = 0
        self.regime_duration = 0

    def update(self, market_state: dict) -> str:
        self.states.append(market_state)
        candidate = self._detect_candidate_regime()
        return self._apply_persistence(candidate)

    def _detect_candidate_regime(self) -> str:
        if len(self.states) < self.min_states:
            return "UNKNOWN"

        recent = list(self.states)
        last = recent[-1]

        trend = last.get("trend_1h", "neutral")
        price = self._safe_float(last.get("close"))
        ema50 = self._safe_float(last.get("ema50"))

        if price is None or ema50 is None:
            return "UNKNOWN"

        valid_ema_states = [
            s for s in recent
            if self._safe_float(s.get("close")) is not None
            and self._safe_float(s.get("ema50")) is not None
        ]

        if not valid_ema_states:
            return "UNKNOWN"

        above_ema_rate = sum(
            1 for s in valid_ema_states
            if self._safe_float(s.get("close")) > self._safe_float(s.get("ema50"))
        ) / len(valid_ema_states)

        below_ema_rate = sum(
            1 for s in valid_ema_states
            if self._safe_float(s.get("close")) < self._safe_float(s.get("ema50"))
        ) / len(valid_ema_states)

        bullish_shifts = [
            s for s in recent
            if str(s.get("trend_shift", "")).startswith("bullish")
            and self._safe_float(s.get("close")) is not None
            and self._safe_float(s.get("ema50")) is not None
        ]

        bearish_shifts = [
            s for s in recent
            if str(s.get("trend_shift", "")).startswith("bearish")
            and self._safe_float(s.get("close")) is not None
            and self._safe_float(s.get("ema50")) is not None
        ]

        bullish_failed = [
            s for s in bullish_shifts
            if self._safe_float(s.get("close")) < self._safe_float(s.get("ema50"))
        ]

        bearish_failed = [
            s for s in bearish_shifts
            if self._safe_float(s.get("close")) > self._safe_float(s.get("ema50"))
        ]

        bullish_fail_rate = (
            len(bullish_failed) / len(bullish_shifts)
            if bullish_shifts else 0
        )

        bearish_fail_rate = (
            len(bearish_failed) / len(bearish_shifts)
            if bearish_shifts else 0
        )

        structure = self._structure_strength(recent)

        up_strength = structure["up_strength"]
        down_strength = structure["down_strength"]
        structure_balance = abs(up_strength - down_strength)

        # =====================================================
        # 1) REGÍMENES FUERTES
        # =====================================================
        sell_rips_strong = (
            trend == "bearish"
            and below_ema_rate >= self.ema_memory_threshold
            and (
                (
                    len(bullish_shifts) >= self.min_shifts
                    and bullish_fail_rate >= self.fail_rate_threshold
                )
                or down_strength >= self.structure_threshold
            )
        )

        if sell_rips_strong:
            return "SELL_RIPS"

        buy_dips_strong = (
            trend == "bullish"
            and above_ema_rate >= self.ema_memory_threshold
            and (
                (
                    len(bearish_shifts) >= self.min_shifts
                    and bearish_fail_rate >= self.fail_rate_threshold
                )
                or up_strength >= self.structure_threshold
            )
        )

        if buy_dips_strong:
            return "BUY_DIPS"

        # =====================================================
        # 2) HARD TREND BIAS
        # Si hay tendencia macro + precio del lado correcto,
        # no dejamos que CHOP/MIXED corte la zona.
        # =====================================================
        strong_bull_context = (
            trend == "bullish"
            and above_ema_rate >= 0.50
            and up_strength >= 0.42
        )

        if strong_bull_context:
            return "BUY_DIPS"

        strong_bear_context = (
            trend == "bearish"
            and below_ema_rate >= 0.50
            and down_strength >= 0.42
        )

        if strong_bear_context:
            return "SELL_RIPS"

        # =====================================================
        # 3) REGÍMENES CONTEXTUALES
        # Pullbacks cerca de EMA en tendencia.
        # =====================================================
        near_or_below_ema = price <= ema50 * 1.005
        near_or_above_ema = price >= ema50 * 0.995

        sell_rips_context = (
            trend == "bearish"
            and below_ema_rate >= 0.42
            and near_or_below_ema
            and down_strength >= 0.40
        )

        if sell_rips_context:
            return "SELL_RIPS"

        buy_dips_context = (
            trend == "bullish"
            and above_ema_rate >= 0.42
            and near_or_above_ema
            and up_strength >= 0.40
        )

        if buy_dips_context:
            return "BUY_DIPS"

        # =====================================================
        # 4) CHOP REAL
        # Solo rango real. No debería comerse una tendencia clara.
        # =====================================================
        is_chop = (
            trend == "neutral"
            and 0.40 < above_ema_rate < 0.60
            and up_strength < 0.40
            and down_strength < 0.40
        )

        if is_chop:
            return "CHOP"

        # =====================================================
        # 5) MIXED REAL
        # Transición/conflicto, no simple pausa de tendencia.
        # =====================================================
        is_mixed = (
            (
                trend == "bullish"
                and below_ema_rate > 0.55
                and up_strength < 0.42
            )
            or (
                trend == "bearish"
                and above_ema_rate > 0.55
                and down_strength < 0.42
            )
            or (
                trend == "neutral"
                and 0.35 < above_ema_rate < 0.65
                and structure_balance < 0.08
            )
        )

        if is_mixed:
            return "MIXED"

        # =====================================================
        # 6) FALLBACK CON SESGO
        # Si trend sigue bullish/bearish, respetamos el contexto.
        # =====================================================
        if trend == "bullish":
            return "BUY_DIPS"

        if trend == "bearish":
            return "SELL_RIPS"

        return "CHOP"

    def _structure_strength(self, states: list[dict]) -> dict:
        valid = [
            s for s in states
            if self._safe_float(s.get("high")) is not None
            and self._safe_float(s.get("low")) is not None
        ]

        if len(valid) < 5:
            return {
                "up_strength": 0.0,
                "down_strength": 0.0,
            }

        higher_highs = 0
        higher_lows = 0
        lower_highs = 0
        lower_lows = 0
        total = 0

        for i in range(1, len(valid)):
            prev_high = self._safe_float(valid[i - 1].get("high"))
            prev_low = self._safe_float(valid[i - 1].get("low"))
            curr_high = self._safe_float(valid[i].get("high"))
            curr_low = self._safe_float(valid[i].get("low"))

            if None in [prev_high, prev_low, curr_high, curr_low]:
                continue

            if curr_high > prev_high:
                higher_highs += 1

            if curr_low > prev_low:
                higher_lows += 1

            if curr_high < prev_high:
                lower_highs += 1

            if curr_low < prev_low:
                lower_lows += 1

            total += 1

        if total == 0:
            return {
                "up_strength": 0.0,
                "down_strength": 0.0,
            }

        up_strength = (higher_highs + higher_lows) / (total * 2)
        down_strength = (lower_highs + lower_lows) / (total * 2)

        return {
            "up_strength": up_strength,
            "down_strength": down_strength,
        }

    def _apply_persistence(self, candidate: str) -> str:
        if candidate == self.current_regime:
            self.regime_duration += 1
            self.pending_regime = None
            self.pending_count = 0
            return self.current_regime

        if candidate == self.pending_regime:
            self.pending_count += 1
        else:
            self.pending_regime = candidate
            self.pending_count = 1

        if self.pending_count >= self.regime_confirm_bars:
            self.current_regime = candidate
            self.regime_duration = 1
            self.pending_regime = None
            self.pending_count = 0

        return self.current_regime

    def _safe_float(self, value):
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None