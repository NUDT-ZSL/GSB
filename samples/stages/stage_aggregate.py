"""Stage D: NumPy aggregation over enriched rows."""
from common import maybe_fail, read_context, read_jsonl, write_jsonl
import numpy as np


def main() -> None:
    stage_id, output_path, input_map, _, attempt_no, fail_config = read_context()
    maybe_fail(stage_id, attempt_no, fail_config)
    rows = read_jsonl(input_map["enrich"])
    values = np.array([row["value"] for row in rows], dtype=np.float64)
    squares = np.array([row["value_squared"] for row in rows], dtype=np.float64)
    summary = {
        "count": int(values.size),
        "mean": round(float(np.mean(values)), 6),
        "stddev": round(float(np.std(values)), 6),
        "mean_squared": round(float(np.mean(squares)), 6),
        "stage": stage_id,
    }
    write_jsonl(output_path, [summary])


if __name__ == "__main__":
    main()
