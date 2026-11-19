from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .executor import execute_chunk_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Minimal one-chunk local worker entry point"
    )
    parser.add_argument("--task-json", required=True)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args(argv)
    task = json.loads(Path(args.task_json).read_text(encoding="utf-8"))
    result = execute_chunk_task(task)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    metadata = {key: value for key, value in result.items() if key != "output"}
    if result.get("ok"):
        np.save(prefix.with_suffix(".npy"), result["output"])
    prefix.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

