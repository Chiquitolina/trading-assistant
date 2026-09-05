class RiskManager:

    def calculate_tp_sl(
        self,
        plan,
        real_entry,
        mark_price,
    ):
        entry = float(plan.entry)
        plan_sl = float(plan.sl)
        plan_tp = float(plan.tp)
        real_entry = float(real_entry)
        mark_price = float(mark_price)

        risk = abs(entry - plan_sl)
        reward = abs(plan_tp - entry)

        # ======================================
        # ORIGINAL MANAGEMENT
        # ======================================

        if plan.side == "LONG":
            original_sl = real_entry - risk
            tp = real_entry + reward

            original_sl = min(
                original_sl,
                mark_price * 0.998,
            )

            tp = max(
                tp,
                mark_price * 1.002,
            )

        else:
            original_sl = real_entry + risk
            tp = real_entry - reward

            original_sl = max(
                original_sl,
                mark_price * 1.002,
            )

            tp = min(
                tp,
                mark_price * 0.998,
            )

        # Por defecto conserva la gestión original.
        sl = original_sl

        context = (
            plan.signal_context
            if isinstance(plan.signal_context, dict)
            else {}
        )

        plan.signal_context = context

        context["hybrid_sl_mode"] = "ORIGINAL"
        context["hybrid_structural_risk_pct"] = None
        context["hybrid_structural_sl_price"] = None
        
        context["hybrid_original_sl_price"] = original_sl
        context["hybrid_sl_reason"] = "ORIGINAL_FALLBACK"

        hybrid_enabled = bool(
            context.get(
                "hybrid_structural_sl_enabled",
                False,
            )
        )

        # ======================================
        # HYBRID STRUCTURAL SL — LONG
        # ======================================

        if hybrid_enabled and plan.side == "LONG":
            raw_compression_low = context.get(
                "compression_low"
            )

            try:
                compression_low = float(
                    raw_compression_low
                )
            except (TypeError, ValueError):
                compression_low = None

            max_structural_risk_pct = float(
                context.get(
                    "hybrid_structural_max_risk_pct",
                    2.0,
                )
            )

            structural_buffer_pct = float(
                context.get(
                    "hybrid_structural_sl_buffer_pct",
                    0.0,
                )
            )

            if (
                compression_low is not None
                and compression_low > 0
            ):
                buffered_structural_sl = (
                    compression_low
                    * (
                        1
                        - structural_buffer_pct
                        / 100
                    )
                )

                # Mantiene la distancia mínima utilizada
                # actualmente respecto del mark price.
                proposed_structural_sl = min(
                    buffered_structural_sl,
                    mark_price * 0.998,
                )

                structural_risk_pct = (
                    (
                        real_entry
                        - proposed_structural_sl
                    )
                    / real_entry
                    * 100
                )

                context[
                    "hybrid_structural_risk_pct"
                ] = round(
                    structural_risk_pct,
                    4,
                )

                context[
                    "hybrid_structural_sl_price"
                ] = proposed_structural_sl

                if (
                    structural_risk_pct > 0
                    and structural_risk_pct
                    <= max_structural_risk_pct
                ):
                    sl = proposed_structural_sl

                    context[
                        "hybrid_sl_mode"
                    ] = "STRUCTURAL"

                    context[
                        "hybrid_sl_reason"
                    ] = (
                        "STRUCTURAL_RISK_WITHIN_CAP"
                    )

                else:
                    context[
                        "hybrid_sl_reason"
                    ] = (
                        "STRUCTURAL_RISK_OUTSIDE_CAP"
                    )

            else:
                context[
                    "hybrid_sl_reason"
                ] = "INVALID_COMPRESSION_LOW"

        context[
            "hybrid_selected_sl_price"
        ] = sl

        print(
            "[HYBRID SL] "
            f"symbol={plan.symbol} | "
            f"mode={context.get('hybrid_sl_mode')} | "
            f"reason={context.get('hybrid_sl_reason')} | "
            f"risk_pct={context.get('hybrid_structural_risk_pct')} | "
            f"original_sl={original_sl} | "
            f"selected_sl={sl} | "
            f"compression_low={context.get('compression_low')}"
        )

        return tp, sl

    def calculate_tp_sl_from_position(
        self,
        side: str,
        entry_price: float,
        mark_price: float,
        tp_pct: float = 0.004,
        sl_pct: float = 0.008
    ):
        if side == "LONG":
            tp = entry_price * (1 + tp_pct)
            sl = entry_price * (1 - sl_pct)

            sl = min(sl, mark_price * 0.998)
            tp = max(tp, mark_price * 1.002)

        else:
            tp = entry_price * (1 - tp_pct)
            sl = entry_price * (1 + sl_pct)

            tp = min(tp, mark_price * 0.998)
            sl = max(sl, mark_price * 1.002)

        return tp, sl