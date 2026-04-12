import math


class PositionSizer:
    def __init__(self, usage_pct=0.85, buffer=0.97, min_notional=105):
        self.usage_pct = usage_pct
        self.buffer = buffer
        self.min_notional = min_notional

    def calculate(self, balance, price, leverage):
        usable_balance = float(balance) * self.usage_pct
        raw_notional = usable_balance * float(leverage) * self.buffer

        # redondeo al step 0.001 de BTC
        quantity = raw_notional / float(price)
        quantity = math.ceil(quantity * 1000) / 1000

        notional = quantity * float(price)
        required_margin = notional / float(leverage)

        # si se pasa del margen usable, bajar un step
        if required_margin > usable_balance:
            quantity = max(0.0, quantity - 0.001)
            quantity = math.floor(quantity * 1000) / 1000

            notional = quantity * float(price)
            required_margin = notional / float(leverage)

        return {
            "quantity": float(quantity),
            "notional": float(notional),
            "required_margin": float(required_margin),
            "usable_balance": float(usable_balance),
        }

    def validate(self, data):
        if data["quantity"] <= 0:
            return False, "❌ Quantity inválida"

        if data["notional"] < self.min_notional:
            return False, f"❌ Notional too small: {data['notional']:.2f}"

        if data["required_margin"] > data["usable_balance"]:
            return False, (
                f"❌ Margin insuficiente: "
                f"required={data['required_margin']:.2f} > usable={data['usable_balance']:.2f}"
            )

        return True, "✅ OK"