import math


class PositionSizer:
    def __init__(self, usage_pct=0.65, buffer=0.90, min_notional=105):
        self.usage_pct = usage_pct
        self.buffer = buffer
        self.min_notional = min_notional

    def calculate(self, balance, price, leverage):
        balance = float(balance)
        price = float(price)
        leverage = float(leverage)

        usable_balance = balance * self.usage_pct
        raw_notional = usable_balance * leverage * self.buffer

        # redondeo hacia abajo al step 0.001 BTC
        quantity = raw_notional / price
        quantity = math.floor(quantity * 1000) / 1000

        notional = quantity * price
        required_margin = notional / leverage

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