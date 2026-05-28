class RiskManager:

    def calculate_tp_sl(self, plan, real_entry, mark_price):
        entry = float(plan.entry)
        plan_sl = float(plan.sl)
        plan_tp = float(plan.tp)
        real_entry = float(real_entry)
        mark_price = float(mark_price)

        risk = abs(entry - plan_sl)
        reward = abs(plan_tp - entry)

        if plan.side == "LONG":
            sl = real_entry - risk
            tp = real_entry + reward

            sl = min(sl, mark_price * 0.998)
            tp = max(tp, mark_price * 1.002)

        else:
            sl = real_entry + risk
            tp = real_entry - reward

            sl = max(sl, mark_price * 1.002)
            tp = min(tp, mark_price * 0.998)

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