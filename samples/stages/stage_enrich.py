"""Stage B: enrich ingest rows deterministically."""
from common import maybe_fail, read_context, read_jsonl, write_jsonl


def main() -> None:
    stage_id, output_path, input_map, _, attempt_no, fail_config = read_context()
    maybe_fail(stage_id, attempt_no, fail_config)
    rows = read_jsonl(input_map["ingest"])
    out = []
    for row in rows:
        enriched = dict(row)
        enriched["value_squared"] = round(row["value"] * row["value"], 6)
        enriched["stage"] = stage_id
        out.append(enriched)
    write_jsonl(output_path, out)


if __name__ == "__main__":
    main()
