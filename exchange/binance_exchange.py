from decimal import ROUND_DOWN, ROUND_UP, Decimal
from time import time, sleep
from binance.client import Client
from exchange.base_exchange import BaseExchange
from decimal import Decimal, ROUND_DOWN, ROUND_UP

class BinanceExchange(BaseExchange):

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.client = Client(api_key, api_secret)

        if testnet:
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

        print("\033[94m[EXCHANGE]\033[0m🔌 Connecting to Binance...")
        self.sync_time()

    def sync_time(self):
        try:
            server_time = self.client.get_server_time()['serverTime']
            self.client.API_TIME_OFFSET = server_time - int(time() * 1000)
            print(f"\033[94m[EXCHANGE]\033[0m ⏱ Binance server time offset: {self.client.API_TIME_OFFSET} ms")
        except Exception as e:
            print(f"❌ Error sincronizando tiempo: {e}")

    def _safe_request(self, func, *args, **kwargs):

        for attempt in range(5):

            try:

                kwargs['timestamp'] = int(time() * 1000 + getattr(self.client, "API_TIME_OFFSET", 0))
                kwargs['recvWindow'] = 5000

                return func(*args, **kwargs)

            except Exception as e:

                msg = str(e)

                if 'code=-1021' in msg or 'Timestamp' in msg:

                    print("⚠️ Timestamp error, resyncing server time...")
                    self.sync_time()
                    sleep(0.2)

                else:
                    raise e

        raise Exception("❌ Request failed after retries due to timestamp")

    def ping(self):
        return self._safe_request(self.client.ping)

    def check_account(self):
        return self._safe_request(self.client.futures_account)

    def get_balance(self):

        account = self._safe_request(self.client.futures_account)

        if account:
            for asset in account["assets"]:
                if asset["asset"] == "USDT":
                    return float(asset["availableBalance"])

        return 0.0
    
    def get_position_size(self, symbol):

        positions = self.client.futures_position_information(symbol=symbol)

        for p in positions:
            if p["symbol"] == symbol:
                return float(p["positionAmt"])

        return 0.0

    def get_mark_price(self, symbol: str) -> float:

        data = self._safe_request(
            self.client.futures_mark_price,
            symbol=symbol
        )

        return float(data["markPrice"])
        
    def get_open_positions(self):
        try:
            positions = self._safe_request(
                self.client.futures_position_information
            )

            result = []

            for pos in positions:
                amount = float(pos.get("positionAmt", 0))

                if amount == 0:
                    continue

                side = "LONG" if amount > 0 else "SHORT"

                result.append({
                    "symbol": pos["symbol"],
                    "side": side,
                    "amount": amount,
                    "quantity": abs(amount),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "mark_price": float(pos.get("markPrice", 0)),
                    "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),
                    "leverage": int(pos.get("leverage", 0)),
                    "isolated": pos.get("isolated", False),
                })

            return result

        except Exception as e:
            print(f"❌ Error obteniendo posiciones abiertas: {e}")
            return []

    def get_position(self, symbol: str):

        try:

            positions = self._safe_request(
                self.client.futures_position_information,
                symbol=symbol
            )

            if not positions:
                return None

            pos = positions[0]

            amt = float(pos.get("positionAmt", 0))

            if abs(amt) == 0:
                return None

            return {
                "symbol": symbol,
                "amount": amt,
                "entry_price": float(pos.get("entryPrice", 0)),
                "unrealized_pnl": float(pos.get("unRealizedProfit", 0))
            }

        except Exception as e:
            print(f"❌ Error obteniendo posición: {e}")
            return None

    def set_leverage(self, symbol: str, leverage: int):

        return self._safe_request(
            self.client.futures_change_leverage,
            symbol=symbol,
            leverage=leverage
        )

    def place_market_order(self, symbol, side, quantity):

        return self._safe_request(
            self.client.futures_create_order,
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity
        )
        
    def place_take_profit_limit(self, symbol, side, quantity, price):
        print(
            f"[EXCHANGE] TP LIMIT send | side={side} "
            f"price={price} qty={quantity}"
        )

        response = self._safe_request(
            self.client.futures_create_order,
            symbol=symbol,
            side=side,
            type="LIMIT",
            quantity=quantity,
            price=price,
            timeInForce="GTC",
            reduceOnly=True
        )

        print(
            f"[EXCHANGE] TP LIMIT created | side={side} "
            f"price={price} response={response}"
        )

        return response
        
        
    def place_take_profit(self, symbol, side, quantity, stop_price):
        print(
            f"[EXCHANGE] TP MARKET send | side={side} "
            f"trigger={stop_price} qty={quantity}"
        )

        response = self._safe_request(
            self.client.futures_create_order,
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=stop_price,
            quantity=quantity,
            reduceOnly=True,
            workingType="MARK_PRICE",
            priceProtect=True
        )

        print(
            f"[EXCHANGE] TP MARKET created | side={side} "
            f"trigger={stop_price} response={response}"
        )

        return response

    def place_stop_loss(self, symbol, side, quantity, stop_price):
        print(
            f"[EXCHANGE] SL MARKET send | side={side} "
            f"trigger={stop_price} qty={quantity}"
        )

        response = self._safe_request(
            self.client.futures_create_order,
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            quantity=quantity,
            reduceOnly=True,
            workingType="MARK_PRICE",
            priceProtect=True
        )

        print(
            f"[EXCHANGE] SL MARKET created | side={side} "
            f"trigger={stop_price} response={response}"
        )

        return response

    def close_position(self, symbol, side, quantity):

        return self._safe_request(
            self.client.futures_create_order,
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            reduceOnly=True
        )
        
    def cancel_all_orders(self, symbol):

        try:

            orders = self.client.futures_get_open_algo_orders(symbol=symbol)

            print(f"\n🔎 Found {len(orders)} conditional orders")

            for o in orders:

                print(
                    f"\033[94m[EXCHANGE]\033[0m Cancel {o['orderType']} | trigger:{o['triggerPrice']} | id:{o['algoId']}"
                )
                

                self.client.futures_cancel_algo_order(
                    symbol=symbol,
                    algoId=o["algoId"]
                )

            print("🧹 Cancel requests sent")

        except Exception as e:
            print(f"⚠️ Cancel error: {e}")
            
    def get_futures_fees(self, symbol="BTCUSDT"):
        data = self.client.futures_commission_rate(symbol=symbol)

        maker = float(data["makerCommissionRate"]) * 100
        taker = float(data["takerCommissionRate"]) * 100

        return {
            "maker": maker,
            "taker": taker
        }
        
    def get_price(self, symbol: str) -> float:
        data = self.client.futures_symbol_ticker(symbol=symbol)
        return float(data["price"])
    
    def get_recent_fills(self, symbol: str, limit: int = 10):
        try:
            trades = self._safe_request(
                self.client.futures_account_trades,
                symbol=symbol,
                limit=limit
            )
            return trades or []
        except Exception as e:
            print(f"❌ Error obteniendo fills: {e}")
            return []

    def adjust_price_to_tick(self, price: float, tick_size: float, side: str = "DOWN") -> Decimal:
        price_dec = Decimal(str(price))
        tick_dec = Decimal(str(tick_size))

        if side == "DOWN":
            adjusted = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_dec
        else:
            adjusted = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_UP) * tick_dec

        return adjusted
    
        
    def get_price_tick_size(self, symbol: str) -> float:
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        return float(f["tickSize"])
        raise ValueError(f"No se encontró tickSize para {symbol}")
    
    def get_quantity_step_size(self, symbol: str) -> float:
        info = self.client.futures_exchange_info()

        for s in info["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        return float(f["stepSize"])

        raise ValueError(f"No se encontró stepSize para {symbol}")


    def normalize_quantity(self, symbol: str, quantity: float) -> str:
        step = Decimal(str(self.get_quantity_step_size(symbol)))
        quantity_dec = Decimal(str(quantity))

        adjusted = (
            (quantity_dec / step)
            .quantize(Decimal("1"), rounding=ROUND_DOWN)
            * step
        )

        decimals = max(0, -step.as_tuple().exponent)

        return f"{adjusted:.{decimals}f}"

    def normalize_price(self, symbol: str, price: float, side: str = "DOWN") -> str:
        tick = Decimal(str(self.get_price_tick_size(symbol)))
        adjusted = self.adjust_price_to_tick(price, float(tick), side)

        # cantidad de decimales según tickSize
        decimals = max(0, -tick.as_tuple().exponent)
        return f"{adjusted:.{decimals}f}"
