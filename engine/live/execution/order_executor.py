class OrderExecutor:
    def __init__(self, exchange):
        self.exchange = exchange

    def set_leverage(self, symbol, leverage):
        return self.exchange.set_leverage(symbol, leverage)

    def market_order(self, symbol, side, quantity):
        return self.exchange.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity
        )

    def cancel_all(self, symbol):
        return self.exchange.cancel_all_orders(symbol)