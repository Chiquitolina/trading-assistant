from dataclasses import dataclass
from typing import Optional


@dataclass
class BTCVelocityContext:
    symbol: str

    # Magnitud absoluta del movimiento
    velocity_15m: Optional[float]
    velocity_1h: Optional[float]

    # Movimiento con signo:
    # positivo = BTC subió
    # negativo = BTC bajó
    signed_move_15m_pct: Optional[float]
    signed_move_1h_pct: Optional[float]

    direction_15m: Optional[str]
    direction_1h: Optional[str]

    # UP_ALIGNED / DOWN_ALIGNED / MIXED / FLAT / UNKNOWN
    direction_alignment: str

    # HEALTHY / CAUTION / DANGEROUS / UNKNOWN
    state: str

    reason: str


class BTCVelocityContextService:
    BTC_SYMBOL = "BTCUSDT"

    DANGER_15M = 0.80
    DANGER_1H = 1.50

    CAUTION_15M = 0.50
    CAUTION_1H = 1.00

    # Relación rápida símbolo-BTC
    CORRELATION_NOT_FOLLOWING = 0.20
    CORRELATION_FOLLOWING = 0.60
    CORRELATION_STRONG = 0.80

    BETA_LOW = 0.50
    BETA_NORMAL = 0.70
    BETA_AMPLIFIED = 1.20

    def evaluate(
        self,
        buffer,
    ) -> BTCVelocityContext:

        candles_1m = buffer.get_candles(
            self.BTC_SYMBOL,
            "1m",
        )

        if candles_1m is None or len(candles_1m) < 61:
            return self._unknown_context(
                "not_enough_btc_data"
            )

        current_close = float(
            candles_1m[-1]["close"]
        )

        close_15m_ago = float(
            candles_1m[-16]["close"]
        )

        close_1h_ago = float(
            candles_1m[-61]["close"]
        )

        # ==========================================
        # MOVIMIENTOS CON SIGNO
        # ==========================================

        signed_move_15m_pct = self._signed_pct_move(
            current=current_close,
            previous=close_15m_ago,
        )

        signed_move_1h_pct = self._signed_pct_move(
            current=current_close,
            previous=close_1h_ago,
        )

        # ==========================================
        # VELOCIDAD ABSOLUTA
        # ==========================================

        velocity_15m = abs(
            signed_move_15m_pct
        )

        velocity_1h = abs(
            signed_move_1h_pct
        )

        # ==========================================
        # DIRECCIONES
        # ==========================================

        direction_15m = self._direction_from_move(
            signed_move_15m_pct
        )

        direction_1h = self._direction_from_move(
            signed_move_1h_pct
        )

        direction_alignment = (
            self._classify_direction_alignment(
                direction_15m=direction_15m,
                direction_1h=direction_1h,
            )
        )

        # ==========================================
        # ESTADO DE VELOCIDAD
        # ==========================================

        state, reason = self._classify_velocity(
            velocity_15m=velocity_15m,
            velocity_1h=velocity_1h,
        )

        return BTCVelocityContext(
            symbol=self.BTC_SYMBOL,

            velocity_15m=round(
                velocity_15m,
                4,
            ),
            velocity_1h=round(
                velocity_1h,
                4,
            ),

            signed_move_15m_pct=round(
                signed_move_15m_pct,
                4,
            ),
            signed_move_1h_pct=round(
                signed_move_1h_pct,
                4,
            ),

            direction_15m=direction_15m,
            direction_1h=direction_1h,

            direction_alignment=(
                direction_alignment
            ),

            state=state,
            reason=reason,
        )

    # ==============================================
    # RELACIÓN ENTRE BTC Y EL TRADE
    # ==============================================

    def evaluate_trade_relationship(
        self,
        context: BTCVelocityContext,
        side: str,
        correlation: Optional[float] = None,
        beta: Optional[float] = None,
    ) -> dict:
        """
        Interpreta el contexto de BTC respecto del lado
        de la operación y de la relación reciente
        símbolo-BTC.

        No bloquea operaciones.
        No aplica scoring.
        Solo devuelve datos para análisis.
        """

        side = str(side).upper()

        if (
            context.state == "UNKNOWN"
            or side not in {"LONG", "SHORT"}
        ):
            return self._unknown_relationship()

        # ==========================================
        # DIRECCIÓN DE BTC RESPECTO DEL TRADE
        # ==========================================

        if side == "LONG":
            aligned_direction = "UP_ALIGNED"
            against_direction = "DOWN_ALIGNED"

        else:
            aligned_direction = "DOWN_ALIGNED"
            against_direction = "UP_ALIGNED"

        if (
            context.direction_alignment
            == aligned_direction
        ):
            trade_alignment = "ALIGNED"

        elif (
            context.direction_alignment
            == against_direction
        ):
            trade_alignment = "AGAINST"

        else:
            trade_alignment = "MIXED"

        # ==========================================
        # VELOCIDAD + ALINEACIÓN
        # ==========================================

        trade_risk_state = (
            self._build_trade_risk_state(
                context_state=context.state,
                trade_alignment=trade_alignment,
            )
        )

        # ==========================================
        # CORRELACIÓN + BETA
        # ==========================================

        relationship_label = (
            self._classify_relationship(
                trade_alignment=trade_alignment,
                correlation=correlation,
                beta=beta,
            )
        )

        return {
            "btc_trade_alignment": trade_alignment,
            "btc_trade_risk_state": trade_risk_state,
            "btc_relationship_label": (
                relationship_label
            ),
        }

    # ==============================================
    # MOVIMIENTO
    # ==============================================

    @staticmethod
    def _signed_pct_move(
        current: float,
        previous: float,
    ) -> float:

        if previous <= 0:
            return 0.0

        return (
            (current - previous)
            / previous
        ) * 100

    @staticmethod
    def _direction_from_move(
        move_pct: float,
    ) -> str:

        if move_pct > 0:
            return "up"

        if move_pct < 0:
            return "down"

        return "flat"

    # ==============================================
    # ALINEACIÓN TEMPORAL DE BTC
    # ==============================================

    @staticmethod
    def _classify_direction_alignment(
        direction_15m: str,
        direction_1h: str,
    ) -> str:

        if (
            direction_15m == "up"
            and direction_1h == "up"
        ):
            return "UP_ALIGNED"

        if (
            direction_15m == "down"
            and direction_1h == "down"
        ):
            return "DOWN_ALIGNED"

        if (
            direction_15m == "flat"
            and direction_1h == "flat"
        ):
            return "FLAT"

        return "MIXED"

    # ==============================================
    # CLASIFICACIÓN DE VELOCIDAD
    # ==============================================

    def _classify_velocity(
        self,
        velocity_15m: float,
        velocity_1h: float,
    ) -> tuple[str, str]:

        if velocity_15m >= self.DANGER_15M:
            return (
                "DANGEROUS",
                "btc_velocity_15m_danger",
            )

        if velocity_1h >= self.DANGER_1H:
            return (
                "DANGEROUS",
                "btc_velocity_1h_danger",
            )

        if velocity_15m >= self.CAUTION_15M:
            return (
                "CAUTION",
                "btc_velocity_15m_caution",
            )

        if velocity_1h >= self.CAUTION_1H:
            return (
                "CAUTION",
                "btc_velocity_1h_caution",
            )

        return (
            "HEALTHY",
            "btc_velocity_normal",
        )

    # ==============================================
    # ESTADO RESPECTO DEL TRADE
    # ==============================================

    @staticmethod
    def _build_trade_risk_state(
        context_state: str,
        trade_alignment: str,
    ) -> str:

        if context_state == "DANGEROUS":
            return f"DANGER_{trade_alignment}"

        if context_state == "CAUTION":
            return f"CAUTION_{trade_alignment}"

        if context_state == "HEALTHY":
            return f"HEALTHY_{trade_alignment}"

        return "UNKNOWN"

    # ==============================================
    # CORRELACIÓN + BETA
    # ==============================================

    def _classify_relationship(
        self,
        trade_alignment: str,
        correlation: Optional[float],
        beta: Optional[float],
    ) -> str:

        if correlation is None or beta is None:
            return "CORRELATION_UNAVAILABLE"

        correlation = float(correlation)
        beta = float(beta)

        # ==========================================
        # RELACIÓN INVERSA FUERTE
        # ==========================================

        if correlation <= -self.CORRELATION_FOLLOWING:
            if abs(beta) >= self.BETA_AMPLIFIED:
                return "INVERSE_AMPLIFIED"

            return "INVERSE_STRONG"

        # ==========================================
        # SIN RELACIÓN RECIENTE
        # ==========================================

        if abs(correlation) <= (
            self.CORRELATION_NOT_FOLLOWING
        ):
            return "DISCONNECTED"

        # ==========================================
        # RELACIÓN DÉBIL O MODERADA
        # ==========================================

        if correlation < self.CORRELATION_FOLLOWING:
            if trade_alignment == "ALIGNED":
                return "WEAKLY_ALIGNED"

            if trade_alignment == "AGAINST":
                return "WEAKLY_AGAINST"

            return "MIXED_RELATION"

        # ==========================================
        # SIGUE CLARAMENTE A BTC
        # ==========================================

        if trade_alignment == "ALIGNED":
            if beta >= self.BETA_AMPLIFIED:
                return "ALIGNED_AMPLIFIED"

            if beta >= self.BETA_NORMAL:
                return "ALIGNED_NORMAL"

            if beta > 0:
                return "ALIGNED_LOW_BETA"

            return "ALIGNED_INCONSISTENT_BETA"

        if trade_alignment == "AGAINST":
            if beta >= self.BETA_AMPLIFIED:
                return "AGAINST_AMPLIFIED"

            if beta > 0:
                return "AGAINST_CORRELATED"

            return "AGAINST_INCONSISTENT_BETA"

        return "CORRELATED_MIXED"

    # ==============================================
    # RESULTADOS VACÍOS
    # ==============================================

    def _unknown_context(
        self,
        reason: str,
    ) -> BTCVelocityContext:

        return BTCVelocityContext(
            symbol=self.BTC_SYMBOL,
            velocity_15m=None,
            velocity_1h=None,
            signed_move_15m_pct=None,
            signed_move_1h_pct=None,
            direction_15m=None,
            direction_1h=None,
            direction_alignment="UNKNOWN",
            state="UNKNOWN",
            reason=reason,
        )

    @staticmethod
    def _unknown_relationship() -> dict:
        return {
            "btc_trade_alignment": "UNKNOWN",
            "btc_trade_risk_state": "UNKNOWN",
            "btc_relationship_label": "UNKNOWN",
        }