import json
import subprocess
import sys

import numpy as np

from matgen.engine import EXIT_GRACEFUL_STOP, GenerationEngine, StopController
from matgen.persistence import build_version_id, list_completed_indices, load_valid_chunk
from matgen.tiling import plan_chunks
from tests.helpers import make_config, make_spec


def _paths(tmp_path):
    return tmp_path / "state", tmp_path / "output", tmp_path / "archive"


def test_full_run_completes_and_exports(tmp_path):
    state, output, archive = _paths(tmp_path)
    config = make_config(state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec(width=32, height=24, grid_rows=2, grid_cols=2)
    result = GenerationEngine(config).run_material(spec, state, output, archive)

    assert result.status == "completed"
    assert result.consistency["passed"] is True
    assert result.tile_count == len(plan_chunks(spec))
    composite = np.load(output / spec.material_id / "composite.npz")
    assert set(composite.files) == {"color", "roughness", "normal", "height"}
    assert (output / spec.material_id / "version.json").exists()


def test_recovery_reuses_valid_chunks(tmp_path):
    state, output, archive = _paths(tmp_path)
    config = make_config(state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec()
    engine = GenerationEngine(config)
    first = engine.run_material(spec, state, output, archive)
    assert first.status == "completed" and first.recovered_indices == []

    second = GenerationEngine(config).run_material(spec, state, output, archive)
    assert second.status == "completed"
    assert second.recovered_indices == list(range(len(plan_chunks(spec))))
    assert second.computed_indices == []


def test_graceful_stop_at_chunk_boundary(tmp_path):
    state, output, archive = _paths(tmp_path)
    config = make_config(concurrency=1, state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec(width=48, height=48, grid_rows=3, grid_cols=3)  # 9 块
    stop = StopController()
    engine = GenerationEngine(config, stop_controller=stop)
    stop.request_stop()
    result = engine.run_material(spec, state, output, archive)

    assert result.status == "interrupted"
    assert result.exit_code == EXIT_GRACEFUL_STOP
    # 停止在分块边界：最多完成一个已提交块，且没有整幅输出被误写。
    assert result.tile_count <= 1
    assert not (output / spec.material_id).exists()
    marker = json.loads((state / spec.material_id / "interrupted.json").read_text(encoding="utf-8"))
    assert marker["version_id"] == build_version_id(spec, config)

    # 恢复后能确定性补齐并完成。
    recovered = GenerationEngine(config).run_material(spec, state, output, archive)
    assert recovered.status == "completed"
    assert len(recovered.recovered_indices) >= 1


def test_force_crash_recovers_from_persisted_only(tmp_path):
    state, output, archive = _paths(tmp_path)
    # 强制终止使用 os._exit，必须在独立子进程中演示，避免杀死测试运行器。
    sample_dir = tmp_path / "samples"
    from matgen.samplegen import generate_samples

    sample_paths = generate_samples(sample_dir)
    cmd = [
        sys.executable,
        "-m",
        "matgen.cli",
        "run",
        "--materials",
        sample_paths["materials"],
        "--config",
        sample_paths["config"],
        "--state-dir",
        str(state),
        "--output-dir",
        str(output),
        "--archive-dir",
        str(archive),
        "--concurrency",
        "1",
        "--force-crash-after",
        "2",
    ]
    import os

    env = dict(os.environ)
    repo_root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path, env=env)
    assert proc.returncode == 137

    # 崩溃后没有报告/整幅输出，只有已原子提交的两个块。
    assert not output.exists() or not (output / "mat_brick").exists()
    # 子进程使用样本中的 mat_brick（4 块），验证其前两块已原子持久化。
    from matgen.model import load_config, load_materials

    sample_config = load_config(sample_paths["config"])
    specs, _ = load_materials(sample_paths["materials"], sample_config)
    brick = next(item for item in specs if item.material_id == "mat_brick")
    brick_version = build_version_id(brick, sample_config)
    valid, corrupt = list_completed_indices(state, "mat_brick", brick_version)
    assert valid == [0, 1] and corrupt == {}

    # 恢复使用与子进程相同的样本材质，确认从已持久化块继续。
    resumed_engine = GenerationEngine(sample_config)
    resumed_result = resumed_engine.run_material(brick, state, output, archive)
    assert resumed_result.status == "completed"
    assert set([0, 1]).issubset(set(resumed_result.recovered_indices))


def test_corrupt_persisted_chunk_triggers_full_redo(tmp_path):
    state, output, archive = _paths(tmp_path)
    config = make_config(state_dir=str(state), output_dir=str(output), archive_dir=str(archive))
    spec = make_spec()
    version = build_version_id(spec, config)
    GenerationEngine(config).run_material(spec, state, output, archive)

    meta_path = state / spec.material_id / "chunk_0000.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["digest"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    result = GenerationEngine(config).run_material(spec, state, output, archive)
    assert result.status == "completed"
    assert any(error.code == "E_CHUNK_DIGEST_MISMATCH" for error in result.errors)
    # 重做后该块重新有效。
    tile, code = load_valid_chunk(state, spec.material_id, 0, version)
    assert code is None and tile is not None
