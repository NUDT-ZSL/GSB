"""Stage E: final join, tolerating missing weak upstreams."""
from common import maybe_fail, read_context, read_jsonl, write_jsonl


def main() -> None:
    stage_id, output_path, input_map, missing, attempt_no, fail_config = read_context()
    maybe_fail(stage_id, attempt_no, fail_config)
    aggregate = read_jsonl(input_map["aggregate"])
    validation = read_jsonl(input_map.get("validate", ""))
    positive_ids = sorted(row["id"] for row in validation if row.get("is_positive"))
    report = dict(aggregate[0]) if aggregate else {}
    report["positive_ids"] = positive_ids
    report["missing_upstreams"] = sorted(missing)
    report["stage"] = stage_id
    write_jsonl(output_path, [report])


if __name__ == "__main__":
    main()
