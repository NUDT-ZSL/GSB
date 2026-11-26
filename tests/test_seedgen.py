import hashlib
from pathlib import Path

from coord_workbench.seedgen import DEFAULT_SEED, generate_all


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def test_seed_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    generate_all(a, seed=DEFAULT_SEED)
    generate_all(b, seed=DEFAULT_SEED)
    assert _tree_digest(a) == _tree_digest(b)


def test_seed_summary(tmp_path: Path) -> None:
    summary = generate_all(tmp_path / "s", seed=DEFAULT_SEED)
    assert summary["nodes"] == 8
    assert summary["events"] == 18
    assert summary["v2_shards"] == 2
    assert (tmp_path / "s" / "topo_v2" / "meta.sqlite").exists()
