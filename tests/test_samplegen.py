"""Sample generator: deterministic output and full scenario coverage."""

import os

import samplegen


def _tree_bytes(root):
    blob = {}
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                blob[os.path.relpath(path, root)] = fh.read()
    return blob


def test_generation_is_deterministic(tmp_path):
    out_a = str(tmp_path / "a")
    out_b = str(tmp_path / "b")
    samplegen.generate(out_a, "seed-x")
    samplegen.generate(out_b, "seed-x")
    assert _tree_bytes(out_a) == _tree_bytes(out_b)


def test_seed_changes_output(tmp_path):
    out_a = str(tmp_path / "a")
    out_b = str(tmp_path / "b")
    samplegen.generate(out_a, "seed-1")
    samplegen.generate(out_b, "seed-2")
    assert _tree_bytes(out_a) != _tree_bytes(out_b)


def test_all_scenarios_generated(samples):
    dirs = samples["dirs"]
    for name in ("basic", "out_of_order", "invalid", "unknown_policy",
                 "interrupt"):
        assert os.path.isdir(dirs[name])
    interrupt = dirs["interrupt"]
    assert os.path.exists(os.path.join(interrupt, "state", "checkpoint.json"))
    assert os.path.exists(os.path.join(interrupt, "state",
                                       "checkpoint.json.tmp"))
    assert os.path.exists(os.path.join(interrupt, "state", "state.lock"))
