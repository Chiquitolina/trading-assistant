from engine.live.execution.strategies.direction_execution_strategy import (
    DirectionExecutionStrategy,
)


class CompressionExecutionStrategy(
    DirectionExecutionStrategy
):

    BE_TRIGGER = None

    TP_TARGET_PCT = 1.0
    STRUCTURAL_SL_BUFFER_PCT = 0.0
    MAX_STRUCTURAL_RISK_PCT = 2.5

    # ==========================================================
    # PREPARE COMPRESSION PLAN
    # ==========================================================
    def prepare_plan(self, plan):
        context = plan.signal_context or {}

        compression_low = context.get(
            "compression_low"
        )

        if compression_low is None:
            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=missing_compression_low"
            )
            return False

        try:
            entry = float(plan.entry)
            compression_low = float(compression_low)
        except (TypeError, ValueError):
            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=invalid_structural_levels "
                f"entry={plan.entry} "
                f"compression_low={compression_low}"
            )
            return False

        if plan.side != "LONG":
            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=unsupported_side "
                f"side={plan.side}"
            )
            return False

        if entry <= 0:
            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=invalid_entry "
                f"entry={entry}"
            )
            return False

        # Para LONG, el SL debe estar debajo de la entrada.
        if compression_low <= 0 or compression_low >= entry:
            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=invalid_compression_low "
                f"entry={entry:.8f} "
                f"compression_low={compression_low:.8f}"
            )
            return False

        structural_sl = compression_low

        structural_risk_pct = (
            (
                entry
                - structural_sl
            )
            / entry
            * 100
        )

        if (
            structural_risk_pct
            > self.MAX_STRUCTURAL_RISK_PCT
        ):
            plan.signal_context = {
                **context,
                "management_profile": (
                    "MODERATE_BO_TP1_STRUCTURAL_SL"
                ),
                "tp_target_pct": self.TP_TARGET_PCT,
                "sl_source": "compression_low",
                "sl_buffer_pct": (
                    self.STRUCTURAL_SL_BUFFER_PCT
                ),
                "structural_risk_pct": round(
                    structural_risk_pct,
                    4,
                ),
                "structural_risk_cap_pct": (
                    self.MAX_STRUCTURAL_RISK_PCT
                ),
                "structural_risk_accepted": False,
                "management_rejection_reason": (
                    "structural_risk_above_cap"
                ),
            }

            print(
                f"[COMPRESSION MANAGEMENT] "
                f"symbol={plan.symbol} "
                f"rejected=structural_risk_above_cap "
                f"risk={structural_risk_pct:.4f}% "
                f"cap={self.MAX_STRUCTURAL_RISK_PCT:.4f}% "
                f"entry={entry:.8f} "
                f"sl={structural_sl:.8f}"
            )
            return False

        tp = entry * (
            1 + self.TP_TARGET_PCT / 100
        )

        # Sobrescribe los niveles ATR del EntryEngine.
        plan.sl = float(structural_sl)
        plan.tp = float(tp)
        plan.sl_pct = round(
            structural_risk_pct,
            4,
        )
        plan.tp_pct = round(
            self.TP_TARGET_PCT,
            4,
        )

        plan.signal_context = {
            **context,
            "management_profile": (
                "MODERATE_BO_TP1_STRUCTURAL_SL"
            ),
            "tp_target_pct": self.TP_TARGET_PCT,
            "sl_source": "compression_low",
            "sl_buffer_pct": (
                self.STRUCTURAL_SL_BUFFER_PCT
            ),
            "structural_sl_price": float(
                structural_sl
            ),
            "structural_risk_pct": round(
                structural_risk_pct,
                4,
            ),
            "structural_risk_cap_pct": (
                self.MAX_STRUCTURAL_RISK_PCT
            ),
            "structural_risk_accepted": True,
            "management_rejection_reason": None,
        }

        print(
            f"[COMPRESSION MANAGEMENT] "
            f"symbol={plan.symbol} "
            f"accepted=True "
            f"entry={entry:.8f} "
            f"tp={tp:.8f} "
            f"sl={structural_sl:.8f} "
            f"risk={structural_risk_pct:.4f}% "
            f"cap={self.MAX_STRUCTURAL_RISK_PCT:.4f}%"
        )

        return True

    # ==========================================================
    # SIGNAL → OPEN / IGNORE
    # ==========================================================
    def on_signal(
        self,
        execution_engine,
        trade_action,
        plan,
    ):
        pos = execution_engine.get_position(
            plan.symbol
        )

        if not pos:
            print("[COMPRESSION] open position")
            return execution_engine.open_position(
                plan
            )

        if pos.side == plan.side:
            print(
                "[COMPRESSION] same side -> HOLD"
            )
            return False

        print(
            "[COMPRESSION] opposite signal ignored"
        )
        return False

    def on_candle_close(
        self,
        execution_engine,
        closed_candle_ts,
        close_price,
    ):
        return