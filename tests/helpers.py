from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any

from sparse_precision.sample_data import finalize_metadata, sample_definitions


@lru_cache(maxsize=None)
def _finalized(case_id: str) -> dict[str, Any]:
    for definition in sample_definitions():
        if definition["case_id"] == case_id:
            return finalize_metadata(definition)
    raise KeyError(case_id)


def metadata_for(case_id: str) -> dict[str, Any]:
    return copy.deepcopy(_finalized(case_id))

