from __future__ import annotations

import json

import pytest

from transcript_governance.codes import Code
from transcript_governance.formats_v1 import parse_v1_text, read_v1
from transcript_governance.formats_v2 import read_v2_dataset, write_v2_dataset
from transcript_governance.loading import load_transcript
from transcript_governance.migrate import (
    V1ToV2Policy,
    V2ToV1Policy,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from transcript_governance.models import Utterance, Word
from transcript_governance.samples import generate_sample_tree


def test_generated_samples_load_from_both_formats(tmp_path) -> None:
    paths = generate_sample_tree(tmp_path / "samples")
    v1 = read_v1(paths.v1)
    v2 = read_v2_dataset(paths.v2_dir)

    assert v1.ok and v1.format_version == "1.0"
    assert v2.ok and v2.format_version == "2.0"
    assert len(v1.utterances) == len(v2.utterances) == 5
    assert [(u.start_ms, u.end_ms) for u in v1.utterances[:4]] == [
        (u.start_ms, u.end_ms) for u in v2.utterances[:4]
    ]
    assert len(v2.utterances[0].words) == 4

    detected_v1 = load_transcript(paths.v1)
    detected_v2 = load_transcript(paths.v2_dir)
    assert detected_v1.format_name == "v1" and detected_v1.ok
    assert detected_v2.format_name == "v2" and detected_v2.ok


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("missing_field.v1.txt", Code.E_FIELD_MISSING),
        ("duplicate_id.v1.txt", Code.E_DUPLICATE_UTTERANCE_ID),
        ("time_regression.v1.txt", Code.E_TIME_REGRESSION),
        ("invalid_interval.v1.txt", Code.E_INVALID_INTERVAL),
        ("bad_header.v1.txt", Code.E_FORMAT_HEADER),
    ],
)
def test_v1_structured_business_codes(tmp_path, fixture: str, expected: str) -> None:
    paths = generate_sample_tree(tmp_path / "samples")
    result = read_v1(paths.invalid_dir / fixture)
    assert not result.ok
    assert result.code == expected
    assert result.diagnostics[0].to_dict()["code"] == expected


def test_v1_field_set_without_version_header_parses() -> None:
    text = "utterance_id|start_ms|end_ms|text|confidence\nX|0|10|hello|0.8\n"
    result = parse_v1_text(text)
    assert result.ok
    assert result.utterances[0].text == "hello"


def test_v2_structured_business_codes(tmp_path) -> None:
    cases = {
        "missing.json": [{"utterance_id": "X"}],
        "dup.json": [
            {"utterance_id": "D", "start_ms": 0, "end_ms": 10, "text": "a"},
            {"utterance_id": "D", "start_ms": 20, "end_ms": 30, "text": "b"},
        ],
        "regress.json": [
            {"utterance_id": "B", "start_ms": 30, "end_ms": 40, "text": "b"},
            {"utterance_id": "A", "start_ms": 10, "end_ms": 20, "text": "a"},
        ],
        "range.json": [{"utterance_id": "R", "start_ms": 30, "end_ms": 20, "text": "r"}],
    }
    expected = {
        "missing.json": Code.E_FIELD_MISSING,
        "dup.json": Code.E_DUPLICATE_UTTERANCE_ID,
        "regress.json": Code.E_TIME_REGRESSION,
        "range.json": Code.E_INVALID_INTERVAL,
    }
    for name, utterances in cases.items():
        path = tmp_path / name
        path.write_text(json.dumps({"format_version": "2.0", "utterances": utterances}), encoding="utf-8")
        result = read_v2_dataset(path)
        assert result.code == expected[name], name


def test_migration_records_defaults_losses_and_preserves_boundaries(tmp_path) -> None:
    paths = generate_sample_tree(tmp_path / "samples")
    v1 = read_v1(paths.v1)
    v2 = read_v2_dataset(paths.v2_dir)

    to_v2 = migrate_v1_to_v2(v1, V1ToV2Policy(default_speaker="S9"))
    assert to_v2.boundaries_preserved
    assert to_v2.utterances[0].speaker == "S9"
    assert any(record.field_name == "speaker" for record in to_v2.filled_defaults)
    assert any(record.field_name == "words" for record in to_v2.filled_defaults)

    to_v1 = migrate_v2_to_v1(v2)
    assert to_v1.boundaries_preserved
    assert all(u.speaker is None and not u.words for u in to_v1.utterances)
    assert any(record.field_name == "speaker" and record.rule == "discard" for record in to_v1.loss_records)
    assert any(record.field_name == "revision" for record in to_v1.loss_records)


def test_migration_flatten_rules_are_explicit() -> None:
    source = [
        Utterance(
            "X",
            100,
            200,
            "original",
            speaker="S1",
            revision=7,
            words=(Word("hello", 100, 150), Word("world", 151, 200)),
        )
    ]
    result = migrate_v2_to_v1(
        source,
        V2ToV1Policy(
            speaker_rule="flatten_prefix",
            words_rule="flatten_timing_tokens",
            revision_rule="flatten_id_suffix",
        ),
    )
    target = result.utterances[0]
    assert target.utterance_id == "X#r7"
    assert target.text.startswith("[S1]")
    assert "hello[100-150]" in target.text
    assert result.boundaries_preserved


def test_v2_missing_confidence_default_has_recorded_source(tmp_path) -> None:
    shard = tmp_path / "shard.json"
    shard.write_text(
        json.dumps(
            {
                "format": "transcript-v2-shard",
                "format_version": "2.0",
                "utterances": [
                    {"utterance_id": "NO-CONF", "start_ms": 1, "end_ms": 2, "text": "x", "revision": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = read_v2_dataset(shard)
    assert loaded.ok
    result = migrate_v2_to_v1(loaded)
    confidence_fill = next(record for record in result.filled_defaults if record.field_name == "confidence")
    assert confidence_fill.value == 1.0
    assert "reader.optional_confidence" in confidence_fill.source


def test_v2_sqlite_roundtrip_detects_metadata_tampering(tmp_path) -> None:
    utterance = Utterance("X", 10, 20, "hello", speaker="S1", revision=3, words=(Word("hello", 10, 20),))
    manifest = write_v2_dataset([utterance], tmp_path / "v2")
    loaded = read_v2_dataset(manifest.parent)
    assert loaded.ok

    import sqlite3

    db = tmp_path / "v2" / "metadata.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("UPDATE utterance_meta SET speaker = 'TAMPERED' WHERE utterance_id = 'X'")
    connection.commit()
    connection.close()

    tampered = read_v2_dataset(manifest.parent)
    assert not tampered.ok
    assert tampered.code == Code.E_METADATA_MISMATCH
