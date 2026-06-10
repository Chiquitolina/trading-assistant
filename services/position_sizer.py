import math


class PositionSizer:
    def __init__(
        self,
        total_usage_pct=0.65,
        max_positions=10,
        buffer=0.90,
        min_notional=105,
        qty_step=0.001,
    ):
        self.total_usage_pct = total_usage_pct
        self.max_positions = max_positions
        self.buffer = buffer
        self.min_notional = min_notional
        self.qty_step = qty_step

    def _round_down_step(self, quantity):
        return math.floor(quantity / self.qty_step) * self.qty_step

    def calculate(self, total_balance, price, leverage, open_positions_count=0):
        total_balance = float(total_balance)
        price = float(price)
        leverage = float(leverage)

        total_usable_margin = total_balance * self.total_usage_pct * self.buffer

        possible_positions = int(
            (total_usable_margin * leverage) / self.min_notional
        )

        effective_max_positions = max(
            1,
            min(self.max_positions, possible_positions)
        )

        if open_positions_count >= effective_max_positions:
            return {
                "quantity": 0.0,
                "notional": 0.0,
                "required_margin": 0.0,
                "usable_balance": float(total_usable_margin),
                "slot_margin": 0.0,
                "max_positions": int(effective_max_positions),
                "reason": "max_positions_reached",
            }

        slot_margin = total_usable_margin / effective_max_positions
        raw_notional = slot_margin * leverage

        quantity = raw_notional / price
        quantity = self._round_down_step(quantity)

        notional = quantity * price
        required_margin = notional / leverage

        return {
            "quantity": float(quantity),
            "notional": float(notional),
            "required_margin": float(required_margin),
            "usable_balance": float(total_usable_margin),
            "slot_margin": float(slot_margin),
            "open_positions_count": int(open_positions_count),
            "max_positions": int(effective_max_positions),
        }

    def validate(self, data):
        if data.get("reason") == "max_positions_reached":
            return False, f"❌ Máximo de posiciones alcanzado: {data.get('max_positions')}"

        if data["quantity"] <= 0:
            return False, "❌ Quantity inválida"

        if data["notional"] < self.min_notional:
            return False, f"❌ Notional too small: {data['notional']:.2f}"

        if data["required_margin"] > data["slot_margin"]:
            return False, (
                f"❌ Margin insuficiente para slot: "
                f"required={data['required_margin']:.2f} > slot={data['slot_margin']:.2f}"
            )

        return True, "✅ OK"