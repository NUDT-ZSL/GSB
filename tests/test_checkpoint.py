import json
import os

import pytest

from sensitrace import checkpoint as checkpoint_module
from sensitrace.atomic import LockUnavailable, file_lock
from sensitrace.checkpoint import RULES_VERSION, STRUCTURE_VERSION, CheckpointStore
from sensitrace.samplegen import generate_samples


def _valid_documents(sample_dir):
    paths = [
        os.path.join(sample_dir, name)
        for name in sorted(os.listdir(sample_dir))
        if name.startswith("sample_") and name != "sample_bad_version.json"
    ]
    documents = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["__source_file__"] = path
        documents.append(raw)
    return documents


@pytest.fixture()
def documents(tmp_path):
    sample_dir = tmp_path / "samples"
    generate_samples(str(sample_dir))
    return _valid_documents(str(sample_dir))


def test_resume_from_checkpoint_reproduces_clean_run(tmp_path, documents):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    engine, applied, _diag, _failed = CheckpointStore.build_from_documents(documents)
    store.save(engine, applied)

    loaded = store.load()
    assert loaded.rebuilt is False
    assert loaded.applied_versions == applied
    restored_dict = loaded.engine.to_dict()
    clean_dict = engine.to_dict()
    assert restored_dict["nodes"] == clean_dict["nodes"]
    assert restored_dict["edges"] == clean_dict["edges"]
    assert restored_dict["diagnostics"] == clean_dict["diagnostics"]


def test_incremental_import_only_applies_new_watermark(tmp_path, documents):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    first_batch = [d for d in documents if d["version"] in ("1.0.0", "1.1.0")]
    second_batch = [d for d in documents if d["version"] == "2.0.0"]

    engine_one, applied_one, _, _ = CheckpointStore.build_from_documents(first_batch)
    store.save(engine_one, applied_one)

    from sensitrace.session import Session

    session = Session(str(workdir))
    paths = [d["__source_file__"] for d in second_batch]
    outcome = session.import_documents(paths)
    assert outcome.resumed_from_checkpoint is True
    assert outcome.applied_versions == ["1.0.0", "1.1.0", "2.0.0"]


def test_structure_version_mismatch_forces_full_rebuild(tmp_path, documents, monkeypatch):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    engine, applied, _, _ = CheckpointStore.build_from_documents(documents)
    store.save(engine, applied)

    original = checkpoint_module.STRUCTURE_VERSION
    monkeypatch.setattr(checkpoint_module, "STRUCTURE_VERSION", original + 1)
    loaded = store.load()
    assert loaded.rebuilt is True
    assert loaded.engine is None
    assert loaded.diagnostics[0].code == "E_CORRUPT_CHECKPOINT"


def test_rules_version_mismatch_forces_full_rebuild(tmp_path, documents, monkeypatch):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    engine, applied, _, _ = CheckpointStore.build_from_documents(documents)
    store.save(engine, applied)

    original = checkpoint_module.RULES_VERSION
    monkeypatch.setattr(checkpoint_module, "RULES_VERSION", original + 1)
    loaded = store.load()
    assert loaded.rebuilt is True
    assert loaded.engine is None


def test_forced_termination_leaves_no_half_written_checkpoint(tmp_path, documents):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    engine, applied, _, _ = CheckpointStore.build_from_documents(documents)

    with pytest.raises(SystemExit):
        store.save(engine, applied, crash_after_tmp=True)

    assert not os.path.exists(store.checkpoint_path)
    leftovers = [name for name in os.listdir(str(workdir)) if name.startswith(".tmp-")]
    assert leftovers == []  # temp file cleaned on the way out

    # Recovery boundary: nothing complete exists, so a fresh start is required.
    loaded = store.load()
    assert loaded.engine is None and loaded.applied_versions == []


def test_lock_conflict_returns_e_lock(tmp_path):
    lock_path = str(tmp_path / "cp" / ".checkpoint.lock")
    with file_lock(lock_path):
        with pytest.raises(LockUnavailable) as excinfo:
            with file_lock(lock_path):
                pass
    assert excinfo.value.diagnostic().code == "E_LOCK"


def test_corrupt_envelope_checksum_is_rejected(tmp_path, documents):
    workdir = tmp_path / "wd"
    store = CheckpointStore(str(workdir))
    engine, applied, _, _ = CheckpointStore.build_from_documents(documents)
    store.save(engine, applied)

    with open(store.checkpoint_path, encoding="utf-8") as handle:
        envelope = json.load(handle)
    envelope["checksum_sha256"] = "0" * 64
    with open(store.checkpoint_path, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle)

    loaded = store.load()
    assert loaded.rebuilt is True
    assert loaded.diagnostics[0].code == "E_CORRUPT_CHECKPOINT"
