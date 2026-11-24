import json

import numpy as np

from matgen.engine import GenerationEngine
from matgen.exporting import material_output_dir
from tests.helpers import make_config, make_spec


def test_same_version_overwrites_in_place(tmp_path):
    state, output, archive = tmp_path / "state", tmp_path / "output", tmp_path / "archive"
    config = make_config(state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec()
    GenerationEngine(config).run_material(spec, state, output, archive)
    out_dir = material_output_dir(output, spec.material_id)
    first_mtime = (out_dir / "composite.npz").stat().st_mtime_ns

    GenerationEngine(config).run_material(spec, state, output, archive)
    assert not archive.exists() or not any(archive.rglob("version.json"))
    assert (out_dir / "composite.npz").exists()
    assert (out_dir / "composite.npz").stat().st_mtime_ns >= first_mtime


def test_version_change_archives_old_output(tmp_path):
    state, output, archive = tmp_path / "state", tmp_path / "output", tmp_path / "archive"
    config = make_config(state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec(seed=1)
    result_v1 = GenerationEngine(config).run_material(spec, state, output, archive)
    assert result_v1.status == "completed"

    spec_v2 = make_spec(seed=2)
    result_v2 = GenerationEngine(config).run_material(spec_v2, state, output, archive)
    assert result_v2.status == "completed"

    archived_versions = list(archive.rglob("version.json"))
    assert len(archived_versions) == 1
    old = json.loads(archived_versions[0].read_text(encoding="utf-8"))
    current = json.loads((material_output_dir(output, spec.material_id) / "version.json").read_text(encoding="utf-8"))
    assert old["version_id"] != current["version_id"]

    composite = np.load(material_output_dir(output, spec.material_id) / "composite.npz")
    assert "normal" in composite.files
