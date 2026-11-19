"""End-to-end and unit tests for the deterministic pipeline."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xlpipe.config import Limits, PipelineConfig
from xlpipe.errors import (
    E_DUP_PAIR,
    E_GRADE_INVALID,
    E_INCONSISTENT,
    E_LANG_UNSUPPORTED,
    E_MAP_FORMAT,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    PipelineError,
)
from xlpipe.pipeline import Pipeline
from xlpipe.resources import ResourceBundle, build_envelope
from xlpipe.sample_gen import generate
from xlpipe.stages import STAGE_ORDER
from xlpipe.store import StageStore
from xlpipe.util import canonical_dumps, read_json
from xlpipe.verify import verify


@pytest.fixture()
def workspace(tmp_path):
    data = tmp_path / "data"
    resources = tmp_path / "resources"
    generate(str(data), str(resources), n_pairs=60, seed=7)
    return {
        "data": str(data / "pairs.jsonl"),
        "resources": str(resources),
        "config": PipelineConfig.load(str(data / "config.json")),
        "tmp": tmp_path,
    }


def run_pipeline(ws, state_name="state"):
    state = ws["tmp"] / state_name
    pipe = Pipeline(ws["data"], ws["resources"], ws["config"], str(state))
    pipe.run()
    return state


def snapshot_bytes(state):
    out = {}
    for stage in STAGE_ORDER:
        committed = StageStore(str(state)).committed(stage)
        path = state / "stages" / stage / f"v{committed['version']:03d}.jsonl"
        out[stage] = path.read_bytes()
    return out


# ---------------- determinism ----------------

def test_rerun_is_byte_identical(workspace):
    state_a = run_pipeline(workspace, "a")
    state_b = run_pipeline(workspace, "b")
    assert snapshot_bytes(state_a) == snapshot_bytes(state_b)


def test_committed_stages_not_recomputed(workspace):
    state = run_pipeline(workspace)
    pipe = Pipeline(workspace["data"], workspace["resources"],
                    workspace["config"], str(state))
    events = pipe.run()
    assert all(e["action"] == "skip_committed" for e in events)


# ---------------- recovery after hard kill ----------------

def test_recovery_after_kill_matches_clean_run(workspace):
    env = dict(os.environ)
    env["XLPIPE_KILL_AT"] = "score:20"
    crashed = workspace["tmp"] / "crashed"
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_RUNNER, workspace["data"],
         workspace["resources"], str(workspace["tmp"] / "data" / "config.json"),
         str(crashed)],
        env=env, capture_output=True)
    assert proc.returncode == 3  # hard kill, no commit of score stage

    store = StageStore(str(crashed))
    assert store.committed("normalize") is not None
    assert store.committed("align") is not None
    assert store.committed("score") is None  # uncommitted work discarded

    Pipeline(workspace["data"], workspace["resources"], workspace["config"],
             str(crashed)).run()
    clean = run_pipeline(workspace, "clean")
    assert snapshot_bytes(crashed) == snapshot_bytes(clean)


_KILL_RUNNER = (
    "import sys; sys.path.insert(0, %r);"
    "from xlpipe.config import PipelineConfig;"
    "from xlpipe.pipeline import Pipeline;"
    "Pipeline(sys.argv[1], sys.argv[2], PipelineConfig.load(sys.argv[3]),"
    " sys.argv[4]).run()" % str(ROOT)
)


def test_kill_before_commit_discards_work(workspace):
    env = dict(os.environ)
    env["XLPIPE_KILL_BEFORE_COMMIT"] = "align"
    crashed = workspace["tmp"] / "crashed2"
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_RUNNER, workspace["data"],
         workspace["resources"], str(workspace["tmp"] / "data" / "config.json"),
         str(crashed)],
        env=env, capture_output=True)
    assert proc.returncode == 4
    store = StageStore(str(crashed))
    assert store.committed("align") is None
    assert not list((crashed / "working").glob("*"))  # wiped on reopen


# ---------------- input validation error codes ----------------

def _write_pairs(tmp_path, pairs):
    path = tmp_path / "bad_pairs.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for p in pairs:
            fh.write(canonical_dumps(p) + "\n")
    return str(path)


def _base_pair(**overrides):
    pair = {
        "pair_id": "p1", "query_id": "q1", "query_lang": "en",
        "doc_lang": "es", "query_text": "climate and energy",
        "doc_text": "clima y energía", "relevance_grade": 2,
    }
    pair.update(overrides)
    return pair


def _expect_error(workspace, pairs, code):
    data = _write_pairs(workspace["tmp"], pairs)
    with pytest.raises(PipelineError) as excinfo:
        Pipeline(data, workspace["resources"], workspace["config"],
                 str(workspace["tmp"] / "err_state")).run()
    err = excinfo.value
    assert err.code == code
    assert err.stage == "normalize"
    assert err.pair_id is not None
    return err


def test_dup_pair(workspace):
    _expect_error(workspace, [_base_pair(), _base_pair()], E_DUP_PAIR)


def test_lang_unsupported(workspace):
    _expect_error(workspace, [_base_pair(query_lang="xx")], E_LANG_UNSUPPORTED)
    _expect_error(workspace, [_base_pair(doc_lang="jp")], E_LANG_UNSUPPORTED)


def test_grade_invalid(workspace):
    _expect_error(workspace, [_base_pair(relevance_grade=7)], E_GRADE_INVALID)
    _expect_error(workspace, [_base_pair(relevance_grade=-1)], E_GRADE_INVALID)
    _expect_error(workspace, [_base_pair(relevance_grade="high")],
                  E_GRADE_INVALID)


def test_map_format_error(workspace, tmp_path):
    resources = tmp_path / "bad_resources"
    os.makedirs(resources)
    for name in ("lang_pairs.json", "weights.json"):
        src = Path(workspace["resources"]) / name
        (resources / name).write_bytes(src.read_bytes())
    bad = {"entries": [{"src": "en", "dst": "es", "term": "climate",
                        "candidates": "clima"}]}  # candidates must be a list
    envelope = build_envelope("translations", 1, bad)
    (resources / "translations.json").write_text(
        canonical_dumps(envelope), encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        ResourceBundle.load(str(resources))
    err = excinfo.value
    assert err.code == E_MAP_FORMAT
    assert err.resource == "translations"
    assert err.version == 1


# ---------------- resource corruption / versioning ----------------

def test_snapshot_corrupt_resource(workspace, tmp_path):
    resources = tmp_path / "corrupt_resources"
    os.makedirs(resources)
    for name in ("lang_pairs.json", "translations.json", "weights.json"):
        src = Path(workspace["resources"]) / name
        (resources / name).write_bytes(src.read_bytes())
    envelope = read_json(resources / "weights.json")
    envelope["payload"]["w_tfidf"] = 1  # tamper without fixing checksum
    (resources / "weights.json").write_text(
        canonical_dumps(envelope), encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        ResourceBundle.load(str(resources))
    err = excinfo.value
    assert err.code == E_SNAPSHOT_CORRUPT
    assert err.resource == "weights"
    assert err.version == 1


def test_resource_version_change_replaces_wholesale(workspace, tmp_path):
    state = run_pipeline(workspace)
    before = {s: StageStore(str(state)).committed(s)["version"]
              for s in STAGE_ORDER}
    # bump translations resource to version 2 with one extra entry
    envelope = read_json(Path(workspace["resources"]) / "translations.json")
    envelope["version"] = 2
    envelope["payload"]["entries"].append(
        {"src": "en", "dst": "es", "term": "wind", "candidates": ["brisa"]})
    from xlpipe.util import hash_obj
    envelope["checksum"] = hash_obj(envelope["payload"])
    (Path(workspace["resources"]) / "translations.json").write_text(
        canonical_dumps(envelope), encoding="utf-8")

    pipe = Pipeline(workspace["data"], workspace["resources"],
                    workspace["config"], str(state))
    events = pipe.run()
    after = {s: StageStore(str(state)).committed(s)["version"]
             for s in STAGE_ORDER}
    assert after["normalize"] == before["normalize"]  # untouched upstream
    assert after["align"] == before["align"] + 1      # wholesale replace
    assert after["score"] == before["score"] + 1
    assert after["summarize"] == before["summarize"] + 1
    committed = [e for e in events if e["action"] == "committed"]
    assert [e["stage"] for e in committed] == ["align", "score", "summarize"]
    # old snapshot files are gone: replacement, not patching
    align_dir = Path(state) / "stages" / "align"
    assert sorted(p.name for p in align_dir.iterdir()) == [
        "v002.jsonl", "v002.meta.json"]


# ---------------- commit control ----------------

def test_stage_busy(workspace):
    state = run_pipeline(workspace)
    store = StageStore(str(state))
    with store.commit_lock(stage="normalize"):
        other = StageStore(str(state))
        with pytest.raises(PipelineError) as excinfo:
            with other.commit_lock(stage="normalize"):
                pass
    assert excinfo.value.code == E_STAGE_BUSY


def test_snapshot_rollback_rejected(workspace):
    state = run_pipeline(workspace)
    store = StageStore(str(state))
    current = store.committed("normalize")["version"]
    with pytest.raises(PipelineError) as excinfo:
        store.commit("normalize", current, b"{}", {})
    assert excinfo.value.code == E_SNAPSHOT_ROLLBACK
    with pytest.raises(PipelineError):
        store.commit("normalize", current - 1 if current > 1 else 0, b"{}", {})


# ---------------- resource limits and degradation ----------------

def test_pair_limit_exceeded(workspace):
    cfg = PipelineConfig(
        languages=workspace["config"].languages,
        fallback_strategy=workspace["config"].fallback_strategy,
        limits=Limits(max_pairs_per_stage=5,
                      max_alignment_candidates=3,
                      max_working_bytes=1 << 20))
    with pytest.raises(PipelineError) as excinfo:
        Pipeline(workspace["data"], workspace["resources"], cfg,
                 str(workspace["tmp"] / "limited")).run()
    err = excinfo.value
    assert err.code == E_RESOURCE_LIMIT
    assert err.stage == "normalize"


def test_memory_limit_degrades_with_marker(workspace):
    cfg = PipelineConfig(
        languages=workspace["config"].languages,
        fallback_strategy=workspace["config"].fallback_strategy,
        limits=Limits(max_pairs_per_stage=1000,
                      max_alignment_candidates=3,
                      max_working_bytes=256))  # tiny: forces spill
    state = workspace["tmp"] / "spilled"
    Pipeline(workspace["data"], workspace["resources"], cfg, str(state)).run()
    store = StageStore(str(state))
    meta = store.load_meta("normalize", store.committed("normalize")["version"])
    assert "mem_spill" in meta["degradations"]
    # spill must not change output bytes
    clean = run_pipeline(workspace, "spill_ref")
    assert snapshot_bytes(state) == snapshot_bytes(clean)


def test_candidate_truncation_marked(workspace):
    cfg = PipelineConfig(
        languages=workspace["config"].languages,
        fallback_strategy=workspace["config"].fallback_strategy,
        limits=Limits(max_pairs_per_stage=1000,
                      max_alignment_candidates=1,
                      max_working_bytes=1 << 20))
    state = workspace["tmp"] / "trunc"
    Pipeline(workspace["data"], workspace["resources"], cfg, str(state)).run()
    store = StageStore(str(state))
    version = store.committed("align")["version"]
    found = False
    for rec in store.iter_snapshot("align", version):
        for item in rec["aligned"]:
            if item.get("trunc"):
                found = True
                assert rec["stats"]["cand_trunc"] >= 1
    assert found, "expected at least one truncated candidate mapping"


# ---------------- verification tool ----------------

def test_verify_ok(workspace):
    state = run_pipeline(workspace)
    report = verify(workspace["data"], workspace["resources"],
                    workspace["config"], str(state))
    assert report["status"] == "ok"
    assert report["pair_count"] == 60
    assert 0.0 <= report["coverage"]["coverage_mean"] <= 1.0


def test_verify_detects_tampering(workspace):
    state = run_pipeline(workspace)
    path = Path(state) / "stages" / "score" / "v001.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["score"] += 1
    lines[0] = canonical_dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        verify(workspace["data"], workspace["resources"],
               workspace["config"], str(state))
    err = excinfo.value
    assert err.code == E_INCONSISTENT
    assert err.category == "checksum"
    assert err.version == 1


def test_verify_detects_ranking_inconsistency(workspace):
    state = run_pipeline(workspace)
    store = StageStore(str(state))
    # forge a ranking with a duplicated pair and fix checksums so only the
    # structural self-check can catch it
    path = Path(state) / "stages" / "summarize" / "v001.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    recs = [json.loads(l) for l in lines]
    recs[1]["pair_id"] = recs[0]["pair_id"]
    data = ("\n".join(canonical_dumps(r) for r in recs) + "\n").encode()
    path.write_bytes(data)
    from xlpipe.util import sha256_hex
    checksum = sha256_hex(data)
    meta_path = Path(state) / "stages" / "summarize" / "v001.meta.json"
    meta = read_json(meta_path)
    meta["checksum"] = checksum
    meta_path.write_text(canonical_dumps(meta), encoding="utf-8")
    manifest = read_json(Path(state) / "manifest.json")
    manifest["stages"]["summarize"]["checksum"] = checksum
    (Path(state) / "manifest.json").write_text(canonical_dumps(manifest),
                                               encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        verify(workspace["data"], workspace["resources"],
               workspace["config"], str(state))
    err = excinfo.value
    assert err.code == E_INCONSISTENT
    assert err.category in ("ranking", "pair_set")


# ---------------- traceability ----------------

def test_mapping_conflicts_recorded(workspace):
    state = run_pipeline(workspace)
    store = StageStore(str(state))
    meta = store.load_meta("align", store.committed("align")["version"])
    conflicts = meta["mapping_conflicts"]
    assert any(c["term"] == "data" and c["src"] == "en" and c["dst"] == "es"
               for c in conflicts)


def test_fallback_categories_present(workspace):
    state = run_pipeline(workspace)
    store = StageStore(str(state))
    version = store.committed("align")["version"]
    vias = set()
    for rec in store.iter_snapshot("align", version):
        for item in rec["aligned"]:
            vias.add(item["via"])
    assert "direct" in vias
    assert "keep_source" in vias      # missing translation fallback
    assert "no_langpair" in vias      # out-of-map language pair fallback
