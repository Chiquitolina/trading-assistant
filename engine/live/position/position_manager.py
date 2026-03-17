class PositionManager:

    def __init__(self, exchange):
        self.exchange = exchange
        self.position = None  # cache local

    def sync(self, symbol: str):
        try:
            pos = self.exchange.get_position(symbol)

            if not pos or float(pos["amount"]) == 0:
                if self.position is not None:
                    print("🔄 Sync: posición cerrada en exchange")
                self.position = None
                return None

            side = "LONG" if float(pos["amount"]) > 0 else "SHORT"

            self.position = {
                "symbol": symbol,
                "side": side,
                "quantity": abs(float(pos["amount"])),
                "entry_price": float(pos["entry_price"])
            }

            return self.position

        except Exception as e:
            print(f"⚠️ Sync error: {e}")
            return self.position  # fallback al último estado conocido

    def has_position(self):
        return self.position is not None