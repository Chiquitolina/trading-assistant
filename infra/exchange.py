import ccxt

def get_exchange():
    return ccxt.binanceusdm({
        "enableRateLimit": True
    })