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

        # protección contra liquidaciones cercanas / mark price
        if plan.side == "LONG":
            sl = min(sl, mark_price * 0.998)
            tp = max(tp, mark_price * 1.002)
        else:
            sl = max(sl, mark_price * 1.002)
            tp = min(tp, mark_price * 0.998)

        return round(tp, 2), round(sl, 2)