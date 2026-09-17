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


def create_long_exchange():
    market_data = FakeMarketData()

    exchange = SimulatedFuturesExchange(
        market_data=market_data,
        initial_balance=1000.0,
    )

    exchange.set_market_timestamp(
        1742032859999
    )

    exchange.place_market_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity=2.0,
    )

    exchange.place_stop_loss(
        symbol="BTCUSDT",
        side="SELL",
        quantity=2.0,
        stop_price=98.0,
    )

    exchange.place_take_profit_limit(
        symbol="BTCUSDT",
        side="SELL",
        quantity=2.0,
        price=102.0,
    )

    return exchange


# =====================================================
# TEST 1 — TAKE PROFIT
# =====================================================

print("\n==============================")
print("TEST 1 — TAKE PROFIT")
print("==============================")

exchange = create_long_exchange()

event = exchange.process_candle(
    symbol="BTCUSDT",
    open_price=100.5,
    high=102.5,
    low=99.5,
    close=102.1,
    timestamp_ms=1742032919999,
)

print("EVENT:", event)

assert event is not None
assert event["exit_reason"] == "TP"
assert event["exit_price"] == 102.0
assert event["gross_pnl"] == 4.0
assert event["ambiguous"] is False

assert (
    exchange.get_position("BTCUSDT")
    is None
)

assert (
    exchange.get_simulated_protective_orders(
        "BTCUSDT"
    )
    == {}
)

fills = exchange.get_recent_fills(
    "BTCUSDT"
)

assert len(fills) == 2
assert fills[-1]["side"] == "SELL"
assert fills[-1]["price"] == 102.0
assert fills[-1]["realizedPnl"] == 4.0

print("TAKE PROFIT OK")


# =====================================================
# TEST 2 — STOP LOSS
# =====================================================

print("\n==============================")
print("TEST 2 — STOP LOSS")
print("==============================")

exchange = create_long_exchange()

event = exchange.process_candle(
    symbol="BTCUSDT",
    open_price=99.5,
    high=100.0,
    low=97.5,
    close=98.2,
    timestamp_ms=1742032919999,
)

print("EVENT:", event)

assert event is not None
assert event["exit_reason"] == "SL"
assert event["exit_price"] == 98.0
assert event["gross_pnl"] == -4.0
assert event["ambiguous"] is False

assert (
    exchange.get_position("BTCUSDT")
    is None
)

assert (
    exchange.get_simulated_protective_orders(
        "BTCUSDT"
    )
    == {}
)

fills = exchange.get_recent_fills(
    "BTCUSDT"
)

assert len(fills) == 2
assert fills[-1]["side"] == "SELL"
assert fills[-1]["price"] == 98.0
assert fills[-1]["realizedPnl"] == -4.0

print("STOP LOSS OK")


# =====================================================
# TEST 3 — TP + SL SAME CANDLE
# =====================================================

print("\n==============================")
print("TEST 3 — AMBIGUOUS")
print("==============================")

exchange = create_long_exchange()

event = exchange.process_candle(
    symbol="BTCUSDT",
    open_price=100.0,
    high=103.0,
    low=97.0,
    close=101.0,
    timestamp_ms=1742032919999,
)

print("EVENT:", event)

assert event is not None

# Conservative V1:
# SL wins when both were touched.
assert event["exit_reason"] == "SL"
assert event["exit_price"] == 98.0
assert event["gross_pnl"] == -4.0
assert event["ambiguous"] is True

assert (
    exchange.get_position("BTCUSDT")
    is None
)

print("AMBIGUOUS CANDLE OK")


print("\n======================================")
print("REPLAY CANDLE EXECUTION OK")
print("======================================")