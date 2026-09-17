from exchange.simulated_futures_exchange import (
    SimulatedFuturesExchange,
)


EPSILON = 1e-9


class FakeMarketData:
    def __init__(self):
        self.prices = {
            "BTCUSDT": 100.0,
        }

    def last_price(self, symbol):
        return self.prices.get(symbol)


def assert_close(actual, expected):
    assert abs(actual - expected) < EPSILON, (
        f"Expected {expected}, got {actual}"
    )


def create_exchange():
    return SimulatedFuturesExchange(
        market_data=FakeMarketData(),
        initial_balance=1000.0,
        taker_fee_pct=0.05,
    )


# =====================================================
# TEST 1 — ENTRY FEE
# =====================================================

print("\n==============================")
print("TEST 1 — ENTRY FEE")
print("==============================")

exchange = create_exchange()

exchange.set_market_timestamp(
    1742032859999
)

exchange.place_market_order(
    symbol="BTCUSDT",
    side="BUY",
    quantity=2.0,
)

# 2 * 100 = 200 USDT
# 0.05% = 0.10 USDT

assert_close(
    exchange.get_wallet_balance(),
    999.9,
)

print(
    "WALLET AFTER ENTRY:",
    exchange.get_wallet_balance(),
)

print("ENTRY FEE OK")


# =====================================================
# TEST 2 — TP ACCOUNTING
# =====================================================

print("\n==============================")
print("TEST 2 — TP ACCOUNTING")
print("==============================")

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

event = exchange.process_candle(
    symbol="BTCUSDT",
    open_price=100.5,
    high=102.5,
    low=99.5,
    close=102.1,
    timestamp_ms=1742032919999,
)

print("EVENT:", event)

# Gross:
# (102 - 100) * 2 = 4

assert_close(
    event["gross_pnl"],
    4.0,
)

# Entry fee:
# 200 * 0.05% = 0.100

assert_close(
    event["entry_fee"],
    0.1,
)

# Exit fee:
# 204 * 0.05% = 0.102

assert_close(
    event["exit_fee"],
    0.102,
)

assert_close(
    event["total_fees"],
    0.202,
)

# Net:
# 4 - 0.202 = 3.798

assert_close(
    event["net_pnl"],
    3.798,
)

# Wallet:
# 1000 + 3.798

assert_close(
    event["wallet_balance"],
    1003.798,
)

assert_close(
    exchange.get_wallet_balance(),
    1003.798,
)

print("TP ACCOUNTING OK")


# =====================================================
# TEST 3 — SL ACCOUNTING
# =====================================================

print("\n==============================")
print("TEST 3 — SL ACCOUNTING")
print("==============================")

exchange = create_exchange()

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

event = exchange.process_candle(
    symbol="BTCUSDT",
    open_price=99.5,
    high=100.0,
    low=97.5,
    close=98.2,
    timestamp_ms=1742032919999,
)

print("EVENT:", event)

assert_close(
    event["gross_pnl"],
    -4.0,
)

assert_close(
    event["entry_fee"],
    0.1,
)

assert_close(
    event["exit_fee"],
    0.098,
)

assert_close(
    event["total_fees"],
    0.198,
)

assert_close(
    event["net_pnl"],
    -4.198,
)

assert_close(
    event["wallet_balance"],
    995.802,
)

assert_close(
    exchange.get_wallet_balance(),
    995.802,
)

print("SL ACCOUNTING OK")


print("\n======================================")
print("REPLAY ACCOUNTING OK")
print("======================================")