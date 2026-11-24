from pathlib import Path

from pdre.canonical import normalize_digest
from pdre.models import SourceRef
from pdre.parsing import parse_jsonl_file, parse_policy_record


def test_v1_seconds_use_sample_clock_not_system_time():
    source = SourceRef("policy_v1", 3, "policies_v1.jsonl")
    record = parse_policy_record(
        {
            "policy_key": "PDRE:Clock",
            "effective_start_sec": -30,
            "effective_end_sec": 30,
            "match_string": "sha256:" + "a" * 64,
            "action_code": "audit",
        },
        source,
        "v1",
        clock_ms=1_000_000,
    )

    assert record.policy_key == "pdre:clock"
    assert record.effective_start_ms == 970_000
    assert record.effective_end_ms == 1_030_000


def test_digest_bit_width_validation():
    assert normalize_digest("A" * 64) == "sha256:" + "a" * 64
    try:
        normalize_digest("sha512:" + "a" * 64)
    except ValueError:
        pass
    else:
        raise AssertionError("sha512 digest with 256 bits must be rejected")


def test_jsonl_rejections_keep_source_and_line(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"event_seq":1,"endpoint_id":"ep1","process_guid":"p1",'
                '"parent_process_guid":null,"policy_key":"k","action_kind":"FILE_WRITE",'
                '"risk_level":10,"observed_ms":1,"payload_digest":"sha256:' + "a" * 64 + '"}',
                '{"event_seq":2,"endpoint_id":"ep1","process_guid":"bad guid!",'
                '"parent_process_guid":null,"policy_key":"k","action_kind":"NOPE",'
                '"risk_level":999,"observed_ms":-2,"payload_digest":"short"}',
                "{not json",
            ]
        ),
        encoding="utf-8",
    )

    records, rejections, read_error = parse_jsonl_file(path, "event")
    assert read_error is None
    assert len(records) == 1
    assert {item.record_seq for item in rejections} == {2, 3}
    assert any(item.code == "E_JSON_DECODE" and item.record_seq == 3 for item in rejections)
    assert any(item.code == "E_FIELD_RISK_LEVEL" for item in rejections)
    assert any(item.source_category == "event" for item in rejections)
