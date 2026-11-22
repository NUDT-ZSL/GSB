"""Minimal integration example.

Run from the repository root with:
    python examples/demo.py
Windows must retain the ``if __name__ == "__main__"`` guard because the
service uses the spawn process start method for parallel node recomputation.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digit_ocr import (
    DigitOCRService,
    ResourcePolicy,
    build_default_config,
    generate_samples,
)


def main() -> None:
    samples = generate_samples(seed=20261123, per_class=2)
    config = build_default_config(seed=20261123)
    with tempfile.TemporaryDirectory() as temporary:
        service = DigitOCRService(
            config,
            Path(temporary),
            ResourcePolicy(max_parallel_nodes=2),
        )
        result = service.recognize_batch(samples, parallel_workers=2)
        print(
            json.dumps(
                {
                    "business_code": "OK",
                    "summary": {
                        "total": result.metrics_artifact["total"],
                        "correct": result.metrics_artifact["correct"],
                        "rejected": result.metrics_artifact["rejected"],
                    },
                    "decisions": [
                        {
                            "image_id": item["image_id"],
                            "predicted_class": item["predicted_class"],
                            "rejected": item["rejected"],
                            "confidence": item["confidence"],
                            "cache_key": item["cache_key"],
                        }
                        for item in result.decisions
                    ],
                    "self_check": service.self_check(result),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
