"""Shared helpers for deterministic sample stage scripts."""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List


def read_context():
    stage_id = os.environ["PIPE_STAGE_ID"]
    output_path = os.environ["PIPE_OUTPUT_PATH"]
    input_map = json.loads(os.environ.get("PIPE_INPUT_MAP", "{}"))
    missing = json.loads(os.environ.get("PIPE_MISSING_UPSTREAMS", "[]"))
    attempt_no = int(os.environ.get("PIPE_ATTEMPT_NO", "1"))
    fail_config = json.loads(os.environ.get("PIPE_FAIL_FIRST_N", "{}") or "{}")
    return stage_id, output_path, input_map, missing, attempt_no, fail_config


def maybe_fail(stage_id: str, attempt_no: int, fail_config: Dict[str, int]) -> None:
    fail_first_n = int(fail_config.get(stage_id, 0))
    if attempt_no <= fail_first_n:
        sys.stderr.write(
            f"injected failure stage={stage_id} attempt={attempt_no} "
            f"fail_first_n={fail_first_n}\n"
        )
        sys.exit(20)


def read_jsonl(path: str) -> List[dict]:
    rows: List[dict] = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8", newline="\n") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
