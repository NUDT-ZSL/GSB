from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mvengine.cli import main
from mvengine.definitions import parse_v1, parse_v2
from mvengine.errors import EngineError
from mvengine.logs import parse_logs
from mvengine.package import load_package
from mvengine.storage import initialize, status
from mvengine.util import digest_file, pretty_json


def _copy_package(source: Path, target: Path) -> Path:
    shutil.copytree(source.parent, target)
    return target / "package.json"


def test_v1_duplicate_name_has_business_code(tmp_path: Path, sample_suite: dict[str, Path]) -> None:
    source = load_package(sample_suite["base"]).v1.path
    value = json.loads(source.read_text(encoding="utf-8"))
    value["views"].append(dict(value["views"][0]))
    bad = tmp_path / "views.json"
    bad.write_text(pretty_json(value), encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        parse_v1(bad)
    assert exc.value.code == "E_V1_DUPLICATE_VIEW"


def test_v2_shard_hash_mismatch(tmp_path: Path, sample_suite: dict[str, Path]) -> None:
    root = tmp_path / "v2"
    shutil.copytree(load_package(sample_suite["base"]).v2.path.parent, root)
    shard = root / "shards" / "views-00.json"
    shard.write_text(shard.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        parse_v2(root)
    assert exc.value.code == "E_SHARD_HASH_MISMATCH"


def test_bad_log_digest(tmp_path: Path, sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    root = tmp_path / "logs"
    shutil.copytree(package.logs.path.parent, root)
    segment = root / "segments" / "seg-0001.jsonl"
    lines = segment.read_text(encoding="utf-8").splitlines()
    line = json.loads(lines[0])
    line["after"]["amount"] = 999.0
    lines[0] = json.dumps(line, sort_keys=True)
    segment.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["segments"][0]["sha256"] = digest_file(segment)
    manifest.write_text(pretty_json(manifest_value), encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        parse_logs(root)
    assert exc.value.code == "E_LOG_DIGEST"


def test_log_mirror_chain_is_validated(tmp_path: Path, sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    root = tmp_path / "logs"
    shutil.copytree(package.logs.path.parent, root)
    segment = root / "segments" / "seg-0001.jsonl"
    lines = segment.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[3])
    event["before"]["amount"] = 24.0
    from mvengine.util import canonical_json, digest_json

    event["content_digest"] = digest_json(
        {key: value for key, value in event.items() if key != "content_digest"}
    )
    lines[3] = canonical_json(event)
    segment.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["segments"][0]["sha256"] = digest_file(segment)
    manifest.write_text(pretty_json(manifest_value), encoding="utf-8")
    with pytest.raises(EngineError) as exc:
        parse_logs(root)
    assert exc.value.code == "E_LOG_STATE_MISMATCH"


def test_invalid_definition_does_not_create_database(
    tmp_path: Path,
    sample_suite: dict[str, Path],
) -> None:
    package_path = _copy_package(sample_suite["base"], tmp_path / "badpkg")
    root = package_path.parent
    v1_path = root / "v1" / "views.json"
    v1_value = json.loads(v1_path.read_text(encoding="utf-8"))
    v1_value["views"].append(dict(v1_value["views"][0]))
    v1_path.write_text(pretty_json(v1_value), encoding="utf-8")
    manifest = json.loads(package_path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["v1/views.json"] = digest_file(v1_path)
    package_path.write_text(pretty_json(manifest), encoding="utf-8")
    db = tmp_path / "state.sqlite"
    code = main(["load", "--db", str(db), "--package", str(package_path)])
    assert code == 2
    assert not db.exists()


def test_invalid_log_does_not_change_committed_state(
    tmp_path: Path,
    sample_suite: dict[str, Path],
) -> None:
    package = load_package(sample_suite["base"])
    db = tmp_path / "state.sqlite"
    initialize(package, db)
    before = status(db)
    log_root = tmp_path / "badlogs"
    shutil.copytree(package.logs.path.parent, log_root)
    segment = log_root / "segments" / "seg-0001.jsonl"
    segment.write_text(segment.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    code = main(["refresh", "--db", str(db), "--logs", str(log_root)])
    assert code == 2
    after = status(db)
    assert after == before

