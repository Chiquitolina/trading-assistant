class PositionSizer:

    def __init__(self, usage_pct=0.90, buffer=0.97, min_notional=10):
        self.usage_pct = usage_pct
        self.buffer = buffer
        self.min_notional = min_notional

    def calculate(self, balance, price, leverage):

        usable_balance = balance * self.usage_pct

        raw_notional = usable_balance * leverage * self.buffer

        quantity = round(raw_notional / price, 3)

        notional = quantity * price
        required_margin = notional / leverage

        # ajuste si se pasa
        if required_margin > usable_balance:
            quantity = round(quantity * 0.98, 3)

            notional = quantity * price
            required_margin = notional / leverage

        return {
            "quantity": quantity,
            "notional": notional,
            "required_margin": required_margin,
            "usable_balance": usable_balance
        }

    def validate(self, data):

        if data["notional"] < self.min_notional:
            return False, f"❌ Notional too small: {data['notional']:.2f}"

        if data["required_margin"] > data["usable_balance"]:
            return False, "❌ Margin insuficiente"

        return True, "✅ OK"