"""Deterministic slow root used to make resource waiting observable."""
import time

from common import maybe_fail, read_context, write_jsonl


def main() -> None:
    stage_id, output_path, _, _, attempt_no, fail_config = read_context()
    maybe_fail(stage_id, attempt_no, fail_config)
    time.sleep(0.4)
    write_jsonl(
        output_path,
        [{"id": 0, "source": "slow_ingest", "stage": stage_id, "value": 42}],
    )


if __name__ == "__main__":
    main()
