"""Component configuration with validation and resource limits."""
from dataclasses import dataclass
from typing import Tuple

from .errors import BusinessError, E_CONFIG_INVALID, E_THRESHOLD_RANGE

CONTROLLED_CLASSES = (0, 1, 2)


@dataclass
class Config:
    iou_threshold: float = 0.5
    classes: Tuple[int, ...] = CONTROLLED_CLASSES
    # v2 sharding / resource limits
    n_shards: int = 4
    max_open_shards: int = 8
    max_resident_candidates: int = 100000
    max_recompute_images: int = 1000
    allow_degrade: bool = True
    # commit batching
    batch_size: int = 4
    # injectable logical clock
    clock_base_ms: int = 1700000000000
    clock_step_ms: int = 250

    def validate(self):
        if not (0.0 <= float(self.iou_threshold) <= 1.0):
            raise BusinessError(
                E_THRESHOLD_RANGE,
                "iou_threshold %r out of range [0, 1]" % (self.iou_threshold,),
                field="iou_threshold",
            )
        for name in (
            "n_shards",
            "max_open_shards",
            "max_resident_candidates",
            "max_recompute_images",
            "batch_size",
        ):
            if int(getattr(self, name)) <= 0:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    "%s must be a positive integer" % name,
                    field=name,
                )
        if not self.classes:
            raise BusinessError(
                E_CONFIG_INVALID, "classes must be a non-empty set", field="classes"
            )
        return self
