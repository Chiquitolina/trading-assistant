from dataclasses import dataclass

from engine.live.strategy.modes.compression_strategy import CompressionStrategy
from engine.live.strategy.modes.bucket_v2_compression_strategy import BucketV2CompressionStrategy
from models.execution_variant import ExecutionVariant


@dataclass(frozen=True)
class BucketPipelineResult:
    variant: ExecutionVariant
    result: object


class BucketPipelineCoordinator:
    def __init__(self, buffer, journal=None, **common):
        self.v1 = CompressionStrategy(
            buffer=buffer, journal=journal, **common,
        )
        self.v2 = BucketV2CompressionStrategy(
            buffer=buffer, journal=journal, **common,
        )

    def evaluate(self, **kwargs):
        return [
            BucketPipelineResult(ExecutionVariant.BUCKET_V1, self.v1.evaluate(**kwargs)),
            BucketPipelineResult(ExecutionVariant.BUCKET_V2, self.v2.evaluate(**kwargs)),
        ]

    def active_watches(self):
        return {"BUCKET_V1": self.v1.active_watches(), "BUCKET_V2": self.v2.active_watches()}

    def reset_stats(self):
        self.v1.reset_stats(); self.v2.reset_stats()

    def alive_watches_count(self):
        return self.v1.alive_watches_count() + self.v2.alive_watches_count()

    def get_stats(self):
        return {"BUCKET_V1": self.v1.get_stats(), "BUCKET_V2": self.v2.get_stats()}
