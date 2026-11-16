"""Stage A: deterministic seed records (NumPy-backed, fixed seed)."""
from common import maybe_fail, read_context, write_jsonl
import numpy as np


def main() -> None:
    stage_id, output_path, _, _, attempt_no, fail_config = read_context()
    maybe_fail(stage_id, attempt_no, fail_config)
    rng = np.random.default_rng(20261117)
    rows = []
    for index in range(8):
        value = float(np.round(rng.normal(loc=10.0, scale=2.0), 6))
        rows.append(
            {
                "id": index,
                "source": "ingest",
                "value": value,
                "stage": stage_id,
            }
        )
    write_jsonl(output_path, rows)


if __name__ == "__main__":
    main()
