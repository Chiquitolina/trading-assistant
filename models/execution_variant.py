from enum import Enum


class ExecutionVariant(str, Enum):
    BUCKET_V1 = "BUCKET_V1"
    BUCKET_V2 = "BUCKET_V2"
    BUCKET_V1_V2 = "BUCKET_V1_V2"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"

    @classmethod
    def parse(cls, value):
        if value is None or isinstance(value, cls):
            return value
        return cls(value)

