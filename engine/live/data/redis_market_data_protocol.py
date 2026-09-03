PROTOCOL_VERSION = "v1"
KEY_PREFIX = f"market-data:{PROTOCOL_VERSION}"

# Estado general del productor
STATUS_KEY = f"{KEY_PREFIX}:status"
HEARTBEAT_KEY = f"{KEY_PREFIX}:heartbeat"

# Evita ejecutar accidentalmente dos productores centrales
PRODUCER_LOCK_KEY = f"{KEY_PREFIX}:producer-lock"

# Los precios intravela no necesitan persistencia
PRICE_CHANNEL = f"{KEY_PREFIX}:price"

# Los cierres deben poder recuperarse si un consumidor se desconecta
CLOSED_CANDLES_STREAM = f"{KEY_PREFIX}:closed-candles"

# Configuración inicial
HISTORY_MAXLEN = 400
CLOSED_STREAM_MAXLEN = 200_000
HEARTBEAT_TTL_SECONDS = 15
PRODUCER_LOCK_TTL_SECONDS = 30


def normalize_symbol(symbol: str) -> str:
    if not symbol:
        raise ValueError("symbol is required")

    return symbol.upper()


def normalize_timeframe(timeframe: str) -> str:
    if not timeframe:
        raise ValueError("timeframe is required")

    return timeframe.lower()


def history_key(symbol: str, timeframe: str) -> str:
    symbol = normalize_symbol(symbol)
    timeframe = normalize_timeframe(timeframe)

    return (
        f"{KEY_PREFIX}:history:"
        f"{symbol}:{timeframe}"
    )


def last_closed_key(symbol: str, timeframe: str) -> str:
    symbol = normalize_symbol(symbol)
    timeframe = normalize_timeframe(timeframe)

    return (
        f"{KEY_PREFIX}:last-closed:"
        f"{symbol}:{timeframe}"
    )


def consumer_cursor_key(
    consumer_name: str,
) -> str:
    if not consumer_name:
        raise ValueError("consumer_name is required")

    return (
        f"{KEY_PREFIX}:consumer-cursor:"
        f"{consumer_name}"
    )