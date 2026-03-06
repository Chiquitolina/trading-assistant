from time import time, sleep
from binance.client import Client
from exchange.base_exchange import BaseExchange


class BinanceExchange(BaseExchange):

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.client = Client(api_key, api_secret)

        if testnet:
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

        print("\n🔌 Connecting to Binance...")
        self.sync_time()

    def sync_time(self):
        try:
            server_time = self.client.get_server_time()['serverTime']
            self.client.API_TIME_OFFSET = server_time - int(time() * 1000)
            print(f"⏱ Binance server time offset: {self.client.API_TIME_OFFSET} ms")
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

    def place_take_profit(self, symbol, side, quantity, stop_price):

        return self._safe_request(
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

    def place_stop_loss(self, symbol, side, quantity, stop_price):

        return self._safe_request(
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
                    f"Cancel {o['orderType']} | trigger:{o['triggerPrice']} | id:{o['algoId']}"
                )

                self.client.futures_cancel_algo_order(
                    symbol=symbol,
                    algoId=o["algoId"]
                )

            print("🧹 Cancel requests sent")

        except Exception as e:
            print(f"⚠️ Cancel error: {e}")