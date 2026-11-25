#!/usr/bin/env python3
"""Run the local acceptance matrix and write a verification report."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binning_engine.cache import ContentCache, make_cache_key
from binning_engine.cli import verify_output
from binning_engine.codes import EngineError
from binning_engine.config import load_config
from binning_engine.config_migration import read_v2_snapshot
from binning_engine.hashing import canonical_hash, sha256_file, write_atomic_json
from binning_engine.pipeline import run_pipeline
from binning_engine.reference import reference_encode
from binning_engine.snapshot import SnapshotStore
from binning_engine.validation import parse_jsonl_file


def expect_code(code, callback):
    try:
        callback()
    except EngineError as error:
        if error.code != code:
            raise AssertionError(f"expected {code}, got {error.code}: {error}")
        return error.to_dict()
    raise AssertionError(f"expected {code}, but no error was raised")


def run_unittest_suite():
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=1, stream=open(os.devnull, "w"))
    result = runner.run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "was_successful": result.wasSuccessful(),
    }


def run_cli(args, expect_returncode=0):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "binning_engine", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if expect_returncode is not None and proc.returncode != expect_returncode:
        raise AssertionError(
            f"CLI {args[:2]} returned {proc.returncode}, expected {expect_returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw_stdout": proc.stdout}
    return proc, payload


def main():
    acceptance_dir = ROOT / "runs" / "acceptance"
    shutil.rmtree(acceptance_dir, ignore_errors=True)
    acceptance_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_fixtures.py")],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    unit = run_unittest_suite()
    if not unit["was_successful"]:
        raise AssertionError(f"unit tests failed: {unit}")

    config_path = ROOT / "fixtures" / "config" / "engine_config.json"
    samples_path = ROOT / "fixtures" / "samples.jsonl"
    config = load_config(config_path)
    records, _ = parse_jsonl_file(samples_path)
    reference = reference_encode(records, config)

    serial = run_pipeline(
        config_path,
        samples_path=samples_path,
        output_dir=acceptance_dir / "serial",
        workers=1,
        use_process_pool=False,
    )
    parallel = run_pipeline(
        config_path,
        samples_path=samples_path,
        output_dir=acceptance_dir / "parallel",
        workers=2,
        batch_size=24,
        use_process_pool=True,
    )
    repeat = run_pipeline(
        config_path,
        samples_path=samples_path,
        output_dir=acceptance_dir / "parallel_repeat",
        workers=2,
        batch_size=24,
        use_process_pool=True,
    )

    reference_checks = []
    for expected, actual in zip(reference["rows"], parallel["encoding_result"]["rows"]):
        for field in ("bins", "sparse_indices", "feature_vector", "quantized_vector"):
            if expected[field] != actual[field]:
                raise AssertionError(f"reference mismatch for row {actual['row_id']} field {field}")
        reference_checks.append(actual["row_id"])

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    hash_results = {
        "serial_encoding": digest(acceptance_dir / "serial" / "encoding_result.json"),
        "parallel_encoding": digest(acceptance_dir / "parallel" / "encoding_result.json"),
        "repeat_encoding": digest(acceptance_dir / "parallel_repeat" / "encoding_result.json"),
        "serial_statistics": digest(acceptance_dir / "serial" / "statistics.json"),
        "parallel_statistics": digest(acceptance_dir / "parallel" / "statistics.json"),
        "repeat_statistics": digest(acceptance_dir / "parallel_repeat" / "statistics.json"),
    }
    byte_consistency = {
        "serial_vs_parallel_encoding": hash_results["serial_encoding"]
        == hash_results["parallel_encoding"],
        "parallel_repeat_encoding": hash_results["parallel_encoding"]
        == hash_results["repeat_encoding"],
        "serial_vs_parallel_statistics": hash_results["serial_statistics"]
        == hash_results["parallel_statistics"],
    }
    if not all(byte_consistency.values()):
        raise AssertionError(f"byte consistency failed: {byte_consistency}")

    recovery_dir = acceptance_dir / "recovery"
    kill_proc, _ = run_cli(
        [
            "run",
            "--samples",
            str(samples_path),
            "--config",
            str(config_path),
            "--output",
            str(recovery_dir),
            "--workers",
            "2",
            "--batch-size",
            "24",
            "--kill-after-batch",
            "2",
        ],
        expect_returncode=-9,
    )
    resume_proc, resume_payload = run_cli(
        [
            "run",
            "--samples",
            str(samples_path),
            "--config",
            str(config_path),
            "--output",
            str(recovery_dir),
            "--workers",
            "2",
            "--batch-size",
            "24",
        ],
        expect_returncode=0,
    )
    recovery_hashes = {
        "encoding_equal": digest(acceptance_dir / "parallel" / "encoding_result.json")
        == digest(recovery_dir / "encoding_result.json"),
        "statistics_equal": digest(acceptance_dir / "parallel" / "statistics.json")
        == digest(recovery_dir / "statistics.json"),
    }
    baseline_checkpoint = next((acceptance_dir / "parallel" / "checkpoints").glob("*.json"))
    recovered_checkpoint = next((recovery_dir / "checkpoints").glob("*.json"))
    recovery_hashes["checkpoint_equal"] = digest(baseline_checkpoint) == digest(
        recovered_checkpoint
    )
    if not all(recovery_hashes.values()):
        raise AssertionError(f"recovery mismatch: {recovery_hashes}")

    verify = verify_output(acceptance_dir / "parallel")
    if not verify["ok"]:
        raise AssertionError(f"output verification failed: {verify}")

    _, dup_payload = run_cli(
        [
            "run",
            "--samples",
            str(ROOT / "fixtures" / "invalid" / "duplicate_row_id.jsonl"),
            "--config",
            str(config_path),
            "--output",
            str(acceptance_dir / "dup"),
            "--serial",
        ],
        expect_returncode=2,
    )
    _, code_payload = run_cli(
        [
            "run",
            "--samples",
            str(samples_path),
            "--config",
            str(ROOT / "fixtures" / "config" / "invalid_encoding_method.json"),
            "--output",
            str(acceptance_dir / "bad-code"),
            "--serial",
        ],
        expect_returncode=2,
    )

    with tempfile.TemporaryDirectory() as tmp:
        cache = ContentCache(Path(tmp) / "cache")
        key = make_cache_key("stats_export", {"acceptance": True})
        cache.put("stats_export", key, {"ok": True})
        corrupt_path = cache._path("stats_export", key)
        envelope = json.loads(corrupt_path.read_text())
        envelope["data"]["ok"] = False
        write_atomic_json(corrupt_path, envelope)
        corrupt_error = expect_code(
            "E_CACHE_CORRUPT", lambda: cache.get("stats_export", key)
        )

        store = SnapshotStore(Path(tmp) / "snapshots")
        store.put("result", {"v": 2}, 2)
        rollback_error = expect_code(
            "E_SNAPSHOT_ROLLBACK", lambda: store.put("result", {"v": 1}, 1)
        )

    _, v2_metadata = read_v2_snapshot(ROOT / "fixtures" / "config" / "binning_config.v2")

    report = {
        "ok": True,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "unit_tests": unit,
        "reference_rows_checked": len(reference_checks),
        "byte_consistency": byte_consistency,
        "hashes": hash_results,
        "recovery": {
            "kill_returncode": kill_proc.returncode,
            "resume_summary": resume_payload,
            **recovery_hashes,
        },
        "output_verification": {"ok": verify["ok"], "file_count": verify["file_count"]},
        "business_codes": {
            "duplicate": dup_payload["error"]["code"],
            "unknown_encoding": code_payload["error"]["code"],
            "cache_corrupt": corrupt_error["code"],
            "snapshot_rollback": rollback_error["code"],
        },
        "migration": {
            "v2_absolute_ms": v2_metadata["absolute_ms"],
            "v2_relative_seconds": v2_metadata["relative_seconds"],
            "version": v2_metadata["version"],
        },
        "self_check": parallel["self_check"],
        "resource_plan": {
            "code": parallel["resource_plan"]["code"],
            "workers": parallel["resource_plan"]["workers"],
            "batch_size": parallel["resource_plan"]["batch_size"],
            "comparison_count": parallel["comparison_count"],
            "estimated_memory_bytes": parallel["resource_plan"]["estimates"][
                "estimated_memory_bytes"
            ],
        },
    }
    verification_dir = ROOT / "verification"
    verification_dir.mkdir(exist_ok=True)
    write_atomic_json(verification_dir / "acceptance_report.json", report)

    markdown = f"""# 实际验证记录

本文件由 `python3 scripts/acceptance_check.py` 生成/复核。

- Python：`{report['python']}` / `{report['platform']}`
- 自动化测试：`{unit['tests_run']}` 个，失败 `{unit['failures']}`，错误 `{unit['errors']}`
- 参考实现逐行比对：`{report['reference_rows_checked']}` 行的分箱、稀疏索引、量化向量与传播向量全部一致
- 串行/并行编码逐字节一致：`{byte_consistency['serial_vs_parallel_encoding']}`
- 两次并行运行逐字节一致：`{byte_consistency['parallel_repeat_encoding']}`
- 串行/并行统计逐字节一致：`{byte_consistency['serial_vs_parallel_statistics']}`
- SIGKILL 返回码：`{-9}`；恢复后编码一致：`{recovery_hashes['encoding_equal']}`；统计一致：`{recovery_hashes['statistics_equal']}`；检查点一致：`{recovery_hashes['checkpoint_equal']}`
- 输出清单校验：`{verify['ok']}`，文件数 `{verify['file_count']}`
- 自检：`ok={parallel['self_check']['ok']}`，受影响键 `{parallel['self_check']['cache_consistency']['affected_count']}`，复用 `{parallel['self_check']['cache_consistency']['reused_count']}`，重算 `{parallel['self_check']['cache_consistency']['recomputed_count']}`
- 资源计划：`{parallel['resource_plan']['code']}`，workers=`{parallel['resource_plan']['workers']}`，batch_size=`{parallel['resource_plan']['batch_size']}`，实际比较次数=`{parallel['comparison_count']}`
- 业务码：重复 row_id=`{report['business_codes']['duplicate']}`，未知编码=`{report['business_codes']['unknown_encoding']}`，缓存损坏=`{report['business_codes']['cache_corrupt']}`，回退=`{report['business_codes']['snapshot_rollback']}`
- v2 元数据：version=`{v2_metadata['version']}`，absolute_ms=`{v2_metadata['absolute_ms']}`，relative_seconds=`{v2_metadata['relative_seconds']}`

## 规范产物 SHA-256

- serial `encoding_result.json`: `{hash_results['serial_encoding']}`
- parallel `encoding_result.json`: `{hash_results['parallel_encoding']}`
- repeat `encoding_result.json`: `{hash_results['repeat_encoding']}`
- serial `statistics.json`: `{hash_results['serial_statistics']}`
- parallel `statistics.json`: `{hash_results['parallel_statistics']}`
- repeat `statistics.json`: `{hash_results['repeat_statistics']}`

复现命令：

```bash
python3 scripts/generate_fixtures.py
python3 -m pytest -q
python3 scripts/acceptance_check.py
```
"""
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "VERIFICATION_RESULTS.md").write_text(markdown, encoding="utf-8")
    print(json.dumps({"ok": True, "report": "verification/acceptance_report.json"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
