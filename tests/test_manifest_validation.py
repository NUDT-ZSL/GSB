from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from multipart_store import cli
from multipart_store.errors import DomainError, ErrorCode
from multipart_store.manifest import load_manifest, parse_manifest

from conftest import SAMPLES, sample_raw


def _mutated(mutator) -> dict:
    raw = copy.deepcopy(sample_raw("primary.json"))
    mutator(raw)
    return raw


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda raw: raw["buckets"][0].__setitem__("name", "Bad_Bucket"), ErrorCode.BUCKET_NAME_INVALID),
        (lambda raw: raw["objects"][0].__setitem__("key", "/absolute"), ErrorCode.OBJECT_KEY_INVALID),
        (lambda raw: raw["sessions"][1].__setitem__("session_id", "sess-main"), ErrorCode.DUPLICATE_SESSION_ID),
        (lambda raw: raw["sessions"][0].__setitem__("part_size_limit", 0), ErrorCode.INVALID_LIMIT),
        (lambda raw: raw["sessions"][0].__setitem__("max_concurrency", -1), ErrorCode.INVALID_LIMIT),
        (lambda raw: raw["sessions"][0].__setitem__("declared_parts", [0, 2]), ErrorCode.DECLARED_PARTS_INVALID),
        (lambda raw: raw["events"][2].__setitem__("event_id", 0), ErrorCode.EVENT_ORDER_INVALID),
        (lambda raw: raw["events"][1].__setitem__("event_id", 1), ErrorCode.DUPLICATE_EVENT_ID),
        (lambda raw: raw["events"][0].__setitem__("part_number", 99), ErrorCode.PART_NUMBER_INVALID),
        (lambda raw: raw["events"][0].__setitem__("logical_time_ms", -1), ErrorCode.LOGICAL_TIME_INVALID),
        (
            lambda raw: raw["events"][0].__setitem__(
                "checksum", "sha256:not-a-hex-digest"
            ),
            ErrorCode.CHECKSUM_FORMAT_INVALID,
        ),
    ],
)
def test_invalid_manifest_returns_distinct_business_code(mutator, code):
    with pytest.raises(DomainError) as caught:
        parse_manifest(_mutated(mutator))
    assert caught.value.code == code


def test_invalid_manifest_does_not_create_store(tmp_path: Path, capsys):
    raw = _mutated(lambda data: data["buckets"][0].__setitem__("name", "No"))
    manifest_path = tmp_path / "invalid.json"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    data_dir = tmp_path / "runtime"

    exit_code = cli.main(["--data-dir", str(data_dir), "load", str(manifest_path)])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["error"]["code"] == ErrorCode.BUCKET_NAME_INVALID
    assert not data_dir.exists()


def test_invalid_json_does_not_create_store(tmp_path: Path, capsys):
    manifest_path = tmp_path / "broken.json"
    manifest_path.write_text("{not json", encoding="utf-8")
    data_dir = tmp_path / "runtime"

    exit_code = cli.main(["--data-dir", str(data_dir), "load", str(manifest_path)])
    captured = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert captured["error"]["code"] == ErrorCode.INVALID_JSON
    assert not data_dir.exists()


def test_declared_parts_may_be_unordered_input():
    raw = _mutated(
        lambda data: data["sessions"][0].__setitem__("declared_parts", [2, 0, 1])
    )
    manifest = parse_manifest(raw)
    assert manifest.sessions[0].declared_parts == (0, 1, 2)


def test_unknown_event_session_is_rejected():
    raw = _mutated(lambda data: data["events"][0].__setitem__("session_id", "nope"))
    with pytest.raises(DomainError) as caught:
        parse_manifest(raw)
    assert caught.value.code == ErrorCode.UNKNOWN_SESSION


def test_manifest_load_is_idempotent(tmp_path: Path):
    from multipart_store.service import MultipartStore

    store = MultipartStore(tmp_path / "store")
    manifest = load_manifest(SAMPLES / "primary.json")
    first = store.load(manifest)
    second = store.load(manifest)
    assert first["loaded"] is True
    assert second["loaded"] is False
    assert second["idempotent"] is True


def test_different_manifest_in_same_store_is_rejected(tmp_path: Path):
    from multipart_store.service import MultipartStore

    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "primary.json"))
    with pytest.raises(DomainError) as caught:
        store.load(load_manifest(SAMPLES / "budget.json"))
    assert caught.value.code == ErrorCode.STORE_CONFLICT
