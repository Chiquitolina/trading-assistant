from exchange.binance_exchange import BinanceExchange
from exchange.fill_query import FillQueryStatus, coerce_fill_query_result


class Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def futures_account_trades(self, **kwargs):
        if self.error:
            raise self.error
        return self.response


def exchange(response=None, error=None):
    item = BinanceExchange.__new__(BinanceExchange)
    item.client = Client(response, error)
    return item


def test_successful_empty_fill_query_is_explicit_success():
    result = exchange([]).get_recent_fills("BTCUSDT")
    assert result.status is FillQueryStatus.SUCCESS
    assert result.fills == ()


def test_502_and_timeout_are_never_empty_success():
    for error in (RuntimeError("502 Bad Gateway"), TimeoutError("timed out")):
        result = exchange(error=error).get_recent_fills("BTCUSDT")
        assert result.status is FillQueryStatus.RETRYABLE_ERROR
        assert result.fills == () and result.error


def test_invalid_response_is_fatal_not_empty_success():
    for response in ({"unexpected": True}, None, ""):
        result = exchange(response).get_recent_fills("BTCUSDT")
        assert result.status is FillQueryStatus.FATAL_ERROR


def test_list_returning_legacy_double_remains_supported():
    result = coerce_fill_query_result([{"id": 1}])
    assert result.ok and result.fills == ({"id": 1},)
