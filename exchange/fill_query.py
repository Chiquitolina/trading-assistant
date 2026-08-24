from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class FillQueryStatus(str, Enum):
    SUCCESS = "SUCCESS"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FATAL_ERROR = "FATAL_ERROR"


@dataclass(frozen=True)
class FillQueryResult:
    status: FillQueryStatus
    fills: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    @classmethod
    def success(cls, fills: Iterable[dict[str, Any]]):
        return cls(FillQueryStatus.SUCCESS, tuple(fills))

    @property
    def ok(self) -> bool:
        return self.status is FillQueryStatus.SUCCESS


def coerce_fill_query_result(value) -> FillQueryResult:
    """Accept list-returning test/legacy exchanges while callers migrate."""
    if isinstance(value, FillQueryResult):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(x, dict) for x in value):
        return FillQueryResult.success(value)
    return FillQueryResult(
        FillQueryStatus.FATAL_ERROR,
        error=f"invalid fill query response: {type(value).__name__}",
    )
