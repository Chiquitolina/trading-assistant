class RiskManager:

    def calculate_tp_sl(self, plan, real_entry, mark_price):
        risk = abs(plan.entry - plan.sl)
        reward = abs(plan.tp - plan.entry)

        if plan.side == "LONG":
            sl = real_entry - risk
            tp = real_entry + reward
        else:
            sl = real_entry + risk
            tp = real_entry - reward

        if plan.side == "LONG":
            sl = min(sl, mark_price * 0.998)
            tp = max(tp, mark_price * 1.002)
        else:
            sl = max(sl, mark_price * 1.002)
            tp = min(tp, mark_price * 0.998)

        return round(tp, 2), round(sl, 2)

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

        return round(tp, 2), round(sl, 2)