import pytest

from xretrieval.canon import canonical_text, checksum
from xretrieval.engine import Engine
from xretrieval.errors import XError
from xretrieval.store import StateStore
from xretrieval.validate import Verifier


@pytest.fixture()
def verified_state(built_v1, paths, tmp_path):
    out = tmp_path / "results.jsonl"
    Engine(built_v1).run_query_file(paths["queries"], str(out))
    return built_v1, out


def test_verify_partitions_and_results(verified_state, paths):
    state, results = verified_state
    verifier = Verifier(state)
    assert verifier.verify_partitions(paths["docs"])["ok"]
    report = verifier.verify_results(paths["queries"], str(results))
    assert report["queries_checked"] == 24


def test_selfcheck_ok(verified_state, paths):
    state, _ = verified_state
    report = Verifier(state).selfcheck(paths["docs"])
    assert report["ok"]
    assert report["bridge_resource_version"] == 1


def test_selfcheck_detects_rebuild_set_mismatch(verified_state, paths,
                                                tmp_path):
    state, _ = verified_state
    import shutil
    incon = tmp_path / "incon"
    shutil.copytree(state, incon)
    # advance to v3 for real, then forge en back onto bridge v1
    Engine(incon).build(paths["v2"], paths["docs"])
    Engine(incon).build(paths["v3"], paths["docs"])
    store = StateStore(incon)
    current = store.load_current()
    current["partitions"]["en"]["bridge_resource_version"] = 1
    current["checksum"] = checksum(
        {k: v for k, v in current.items() if k != "checksum"})
    (incon / "CURRENT.json").write_text(canonical_text(current),
                                        encoding="utf-8")
    store.append_commit({"kind": "batch", "bridge": current["bridge"],
                         "partitions": current["partitions"]})
    with pytest.raises(XError) as excinfo:
        Verifier(incon).selfcheck(paths["docs"])
    assert excinfo.value.code == "E_INCONSISTENT"
    assert excinfo.value.context["category"] == "REBUILD_SET_MISMATCH"
    assert excinfo.value.context["partition"] == "en"


def test_verify_detects_tampered_result(verified_state, paths, tmp_path):
    state, results = verified_state
    lines = results.read_text(encoding="utf-8").splitlines()
    import json
    row = json.loads(lines[0])
    row["results"][0]["score"] += 1.0
    lines[0] = json.dumps(row, sort_keys=True)
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(XError) as excinfo:
        Verifier(state).verify_results(paths["queries"], str(tampered))
    assert excinfo.value.code == "E_INCONSISTENT"
