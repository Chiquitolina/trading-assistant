class PositionManager:

    def __init__(self, exchange):
        self.exchange = exchange
        self.positions = {}  # cache por símbolo

    def sync(self, symbol: str):
        try:
            pos = self.exchange.get_position(symbol)
            
            if pos == "INVALID_SYMBOL":
                return "INVALID_SYMBOL"

            if not pos or float(pos["amount"]) == 0:
                if symbol in self.positions:
                    print(f"🔄 Sync: posición cerrada en exchange | symbol={symbol}")
                    self.positions.pop(symbol, None)

                return None

            side = "LONG" if float(pos["amount"]) > 0 else "SHORT"

            self.positions[symbol] = {
                "symbol": symbol,
                "side": side,
                "quantity": abs(float(pos["amount"])),
                "entry_price": float(pos["entry_price"])
            }

            return self.positions[symbol]

        except Exception as e:
            if "-1121" in str(e) or "Invalid symbol" in str(e):
                print(f"⚠️ Sync skipped invalid symbol | {symbol}")
                return "INVALID_SYMBOL"

            print(f"⚠️ Sync error | symbol={symbol} | error={e}")
            return self.positions.get(symbol)

    def has_position(self, symbol: str):
        return symbol in self.positions

    def get_position(self, symbol: str):
        return self.positions.get(symbol)