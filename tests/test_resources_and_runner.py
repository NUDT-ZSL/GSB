import json

import pytest

from injection_workbench.analyzer import MODE_EXACT
from injection_workbench.checkpoints import CheckpointStore
from injection_workbench.config import Config, Thresholds, Budget
from injection_workbench.resources import (
    Deadline, DeadlineExceeded, BudgetExceeded, LockConflict, file_lock,
)
from injection_workbench.runner import process_manifest, run_batch
from injection_workbench.samples import generate_all


def test_deadline_uses_real_wall_clock():
    import time
    deadline = Deadline(0.05)
    time.sleep(0.08)
    with pytest.raises(DeadlineExceeded) as exc:
        deadline.check()
    assert exc.value.error_code == "E_TIMEOUT"
    assert exc.value.elapsed >= 0.05


def test_budget_exceeded_for_too_many_paths(tmp_dirs, config):
    samples, _, _ = tmp_dirs
    generate_all(samples)
    config.budget.max_paths_per_manifest = 10
    with pytest.raises(BudgetExceeded) as exc:
        process_manifest(samples / "manifest_large.json", config)
    assert exc.value.error_code == "E_BUDGET"


def test_timeout_isolated_and_recorded(tmp_dirs):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    # 放大样本到足够处理量，使 0.5ms 的真实墙钟上限必然在循环中触发
    big = json.loads((samples / "manifest_large.json").read_text(encoding="utf-8"))
    big["paths"] = big["paths"] * 20
    (samples / "manifest_huge.json").write_text(json.dumps(big), encoding="utf-8")
    config = Config(
        thresholds=Thresholds(path_count=10, sink_count=5),
        # 每份清单独立墙钟预算：上万路径的大清单必然超 2ms，十几条路径的小清单不会
        budget=Budget(concurrency=1, deadline_seconds=0.002, max_paths_per_manifest=1000000),
    )
    store = CheckpointStore(checkpoints, config)
    report = run_batch(
        [samples / "manifest_huge.json", samples / "manifest_small.json"],
        config, checkpoint_store=store, report_path=reports / "r.json",
    )
    codes = {item.get("error_code") for item in report["manifests"]}
    assert "E_TIMEOUT" in codes
    # 同批其他清单不受影响
    statuses = {item["status"] for item in report["manifests"]}
    assert "ok" in statuses


def test_lock_conflict_returns_e_lock(tmp_path):
    lock_path = tmp_path / "writer.lock"
    with file_lock(str(lock_path)):
        with pytest.raises(LockConflict) as exc:
            with file_lock(str(lock_path)):
                pass
    assert exc.value.error_code == "E_LOCK"


def test_report_lock_conflict_recorded_not_fatal(tmp_dirs, config):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    store = CheckpointStore(checkpoints, config)
    # 预先占用报告目录锁
    with file_lock(str(reports / ".report.lock")):
        report = run_batch(
            [samples / "manifest_small.json"], config,
            checkpoint_store=store, report_path=reports / "r.json",
        )
    assert report["summary"]["lock_error"]["error_code"] == "E_LOCK"
    # 分析结果仍然完整返回
    assert report["summary"]["ok"] == 1


def test_batch_report_structure_and_modes(tmp_dirs):
    samples, reports, checkpoints = tmp_dirs
    generate_all(samples)
    config = Config()  # 默认阈值：small 走精确模式，large 走批处理模式
    store = CheckpointStore(checkpoints, config)
    report_path = reports / "report.json"
    report = run_batch(
        sorted(samples.glob("*.json")), config,
        checkpoint_store=store, report_path=report_path,
    )
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk["summary"]["total_manifests"] == 3
    by_file = {item["file_id"]: item for item in report["manifests"]}
    assert by_file["f-small"]["mode"] == MODE_EXACT
    assert by_file["f-large"]["mode"] == "batch_grouped"
    assert by_file["f-large"]["equivalence_pruning"]["memo_hits"] > 0
    # 非法清单的拒绝记录被保留
    assert by_file["f-invalid"]["rejections"]
