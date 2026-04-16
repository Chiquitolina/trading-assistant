import json
import time
import websocket

TEST_DURATION = 10  # segundos por URL

URLS = [
    ("FUTURES_KLINE", "wss://fstream.binance.com/ws/btcusdt@kline_1m"),
    ("FUTURES_AGGTRADE", "wss://fstream.binance.com/ws/btcusdt@aggTrade"),
    ("FUTURES_MULTIPLEX", "wss://fstream.binance.com/stream?streams=btcusdt@trade"),

    ("FUTURES_ALT", "wss://fstream.binancefuture.com/ws/btcusdt@trade"),
    ("FUTURES_ALT_MULTIPLEX", "wss://fstream.binancefuture.com/stream?streams=btcusdt@trade"),

    ("FUTURES_TESTNET", "wss://stream.binancefuture.com/ws/btcusdt@trade"),

    ("SPOT", "wss://stream.binance.com:9443/ws/btcusdt@trade"),
]


def test_url(name, url):
    print("\n==============================")
    print(f"🔌 TEST: {name}")
    print(f"URL: {url}")

    received = {"count": 0}
    start_time = time.time()

    def on_open(ws):
        print(f"✅ OPENED [{name}]")

    def on_message(ws, message):
        received["count"] += 1

        print(f"\n📩 [{name}] MSG #{received['count']}")
        print("RAW:", message[:200])

        try:
            data = json.loads(message)
            payload = data.get("data", data)
            k = payload.get("k", {})

            print(
                "EVENT:", payload.get("e"),
                "| TF:", k.get("i"),
                "| CLOSED:", k.get("x"),
                "| PRICE:", k.get("c")
            )
        except Exception as e:
            print("PARSE ERROR:", repr(e))

        if time.time() - start_time > TEST_DURATION:
            print(f"⏱️ [{name}] Test terminado")
            ws.close()

    def on_error(ws, error):
        print(f"❌ [{name}] ERROR:", repr(error))

    def on_close(ws, code, msg):
        print(f"🔴 [{name}] CLOSED:", code, msg)

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    ws.run_forever(
        ping_interval=20,
        ping_timeout=10,
    )

    if received["count"] == 0:
        print(f"⚠️ [{name}] OPENED pero NO recibió mensajes")


for name, url in URLS:
    test_url(name, url)
    time.sleep(2)