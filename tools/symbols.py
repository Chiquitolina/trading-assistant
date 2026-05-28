from exchange.binance_exchange import BinanceExchange
from dotenv import load_dotenv
import os

load_dotenv()

exchange = BinanceExchange(
    api_key=os.getenv("API_KEY"),
    api_secret=os.getenv("SECRET_KEY"),
    testnet=False
)

symbols = exchange.get_futures_symbols()

print("SYMBOLS = [")

for symbol in symbols:
    print(f'    "{symbol}",')

print("]")