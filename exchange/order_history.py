from dataclasses import dataclass
from enum import Enum
from typing import Any


class HistoryQueryStatus(str, Enum):
    SUCCESS = "SUCCESS"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FATAL_ERROR = "FATAL_ERROR"


@dataclass(frozen=True)
class OrderHistoryResult:
    status: HistoryQueryStatus
    orders: tuple[dict[str, Any], ...] = ()
    algo_orders: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    @property
    def ok(self):
        return self.status is HistoryQueryStatus.SUCCESS

    @classmethod
    def success(cls, orders=(), algo_orders=()):
        return cls(HistoryQueryStatus.SUCCESS, tuple(orders), tuple(algo_orders))


def classify_query_error(error):
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    message = str(error).lower()
    retryable = (
        isinstance(error, (TimeoutError, ConnectionError))
        or (isinstance(status_code, int) and status_code >= 500)
        or any(token in message for token in (
            "timeout", "timed out", "bad gateway", "temporarily unavailable",
            "connection reset", "connection aborted",
        ))
    )
    return HistoryQueryStatus.RETRYABLE_ERROR if retryable else HistoryQueryStatus.FATAL_ERROR
