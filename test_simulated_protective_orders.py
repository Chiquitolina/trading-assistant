from exchange.simulated_futures_exchange import (
    SimulatedFuturesExchange,
)


class FakeMarketData:
    def __init__(self):
        self.prices = {
            "BTCUSDT": 100.0,
        }

    def last_price(self, symbol):
        return self.prices.get(symbol)


market_data = FakeMarketData()

exchange = SimulatedFuturesExchange(
    market_data=market_data,
    initial_balance=1000.0,
)

# Timestamp histórico ficticio.
exchange.set_market_timestamp(
    1742032859999
)

# =====================================================
# 1. SIMULAR ENTRY
# =====================================================

entry_order = exchange.place_market_order(
    symbol="BTCUSDT",
    side="BUY",
    quantity=2.0,
)

print("\nENTRY:")
print(entry_order)

position = exchange.get_position(
    "BTCUSDT"
)

print("\nPOSITION:")
print(position)

assert position is not None
assert position["amount"] == 2.0
assert position["entry_price"] == 100.0


# =====================================================
# 2. REGISTRAR STOP LOSS
# =====================================================

sl_order = exchange.place_stop_loss(
    symbol="BTCUSDT",
    side="SELL",
    quantity=2.0,
    stop_price=98.0,
)

print("\nSTOP LOSS:")
print(sl_order)


# =====================================================
# 3. REGISTRAR TAKE PROFIT
# =====================================================

tp_order = exchange.place_take_profit_limit(
    symbol="BTCUSDT",
    side="SELL",
    quantity=2.0,
    price=102.0,
)

print("\nTAKE PROFIT:")
print(tp_order)


# =====================================================
# 4. LEER ÓRDENES PROTECTORAS
# =====================================================

orders = (
    exchange.get_simulated_protective_orders(
        "BTCUSDT"
    )
)

print("\nPROTECTIVE ORDERS:")
print(orders)


# =====================================================
# 5. VALIDACIONES
# =====================================================

assert "SL" in orders
assert "TP" in orders

assert (
    orders["SL"]["stopPrice"]
    == 98.0
)

assert (
    orders["SL"]["side"]
    == "SELL"
)

assert (
    orders["SL"]["quantity"]
    == 2.0
)

assert (
    orders["TP"]["price"]
    == 102.0
)

assert (
    orders["TP"]["side"]
    == "SELL"
)

assert (
    orders["TP"]["quantity"]
    == 2.0
)

assert (
    orders["SL"]["reduceOnly"]
    is True
)

assert (
    orders["TP"]["reduceOnly"]
    is True
)


# =====================================================
# 6. FILLS
# =====================================================

fills = exchange.get_recent_fills(
    "BTCUSDT"
)

print("\nFILLS:")
print(fills)

assert len(fills) == 1

assert fills[0]["side"] == "BUY"
assert fills[0]["price"] == 100.0
assert fills[0]["qty"] == 2.0


print(
    "\n======================================"
)
print("REPLAY PROTECTIVE ORDERS OK")
print(
    "======================================"
)