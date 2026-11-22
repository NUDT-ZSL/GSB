from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .dashboard import build_dashboard
from .errors import BusinessError, E_IO, E_RECOVERY_MISMATCH
from .serialization import (
    array_equal_bytes,
    atomic_write_json,
    decode_array,
)
from .snapshots import SnapshotStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_result(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_IO,
            f"无法读取演示结果: {path}",
            stage="demo",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def _run_command(args: list[str], *, expect_exit: int = 0) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != expect_exit:
        raise BusinessError(
            E_IO,
            "演示子进程退出码不符合预期",
            stage="demo",
            details={
                "expected_exit": expect_exit,
                "actual_exit": completed.returncode,
                "command": args,
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            },
        )
    text = completed.stdout.strip()
    if not text:
        return {"returncode": completed.returncode, "stdout": completed.stdout}
    try:
        return json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return {"returncode": completed.returncode, "stdout": completed.stdout}


def compare_full_and_resumed(full: dict[str, Any], resumed: dict[str, Any]) -> dict[str, Any]:
    full_layers = [
        (item["layer_id"], item["activation_checksum"])
        for item in full["inference"]["layers"]
    ]
    resumed_layers = [
        (item["layer_id"], item["activation_checksum"])
        for item in resumed["inference"]["layers"]
    ]
    quant_equal = [
        item["dequantized_checksum"] for item in full["quantization"]["layers"]
    ] == [
        item["dequantized_checksum"] for item in resumed["quantization"]["layers"]
    ]
    calibration_equal = [
        item["output_checksum"] for item in full["calibration"]["layers"]
    ] == [
        item["output_checksum"] for item in resumed["calibration"]["layers"]
    ]
    prediction_equal = (
        full["inference"]["final_prediction_checksum"]
        == resumed["inference"]["final_prediction_checksum"]
        and full["inference"]["final_prediction"]
        == resumed["inference"]["final_prediction"]
    )
    activation_equal = full_layers == resumed_layers
    return {
        "activation_checksums_equal": activation_equal,
        "prediction_equal": prediction_equal,
        "quantized_weights_equal": quant_equal,
        "calibration_outputs_equal": calibration_equal,
        "initial_input_equal": full["initial_input_checksum"]
        == resumed["initial_input_checksum"],
        "full_layer_checksums": dict(full_layers),
        "resumed_layer_checksums": dict(resumed_layers),
        "full_prediction_checksum": full["inference"][
            "final_prediction_checksum"
        ],
        "resumed_prediction_checksum": resumed["inference"][
            "final_prediction_checksum"
        ],
        "resume_from": resumed["recovery"].get("resume_from"),
        "reused_inference_layers": resumed["execution"][
            "reused_layers_by_stage"
        ]["inference"],
        "recomputed_inference_layers": resumed["execution"][
            "recomputed_layers_by_stage"
        ]["inference"],
        "all_equal": (
            activation_equal
            and prediction_equal
            and quant_equal
            and calibration_equal
            and full["initial_input_checksum"] == resumed["initial_input_checksum"]
        ),
    }


def compare_snapshot_tensors(
    full_state: Path,
    full_temp: Path,
    recovery_state: Path,
    recovery_temp: Path,
) -> dict[str, Any]:
    full_store = SnapshotStore(full_state, full_temp)
    recovery_store = SnapshotStore(recovery_state, recovery_temp)
    full_quant = full_store.stage_snapshot("quantization", required=True)
    recovery_quant = recovery_store.stage_snapshot("quantization", required=True)
    full_inference = full_store.stage_snapshot("inference", required=True)
    recovery_inference = recovery_store.stage_snapshot("inference", required=True)
    assert full_quant and recovery_quant and full_inference and recovery_inference
    full_quant_records = {
        item["layer_id"]: item for item in full_quant.payload["records"]
    }
    recovery_quant_records = {
        item["layer_id"]: item for item in recovery_quant.payload["records"]
    }
    layer_bytes: dict[str, Any] = {}
    quant_bytes: dict[str, Any] = {}
    for layer_id, full_record in full_quant_records.items():
        recovery_record = recovery_quant_records[layer_id]
        quant_bytes[layer_id] = {
            "dequantized_bytes_equal": array_equal_bytes(
                decode_array(full_record["dequantized"]),
                decode_array(recovery_record["dequantized"]),
            ),
            "scale_bytes_equal": array_equal_bytes(
                decode_array(full_record["scales"]),
                decode_array(recovery_record["scales"]),
            ),
            "code_bytes_equal": array_equal_bytes(
                decode_array(full_record["codes"]),
                decode_array(recovery_record["codes"]),
            ),
        }
    for layer_id, full_item in full_inference.payload["activations"].items():
        recovery_item = recovery_inference.payload["activations"][layer_id]
        layer_bytes[layer_id] = array_equal_bytes(
            decode_array(full_item["array"]),
            decode_array(recovery_item["array"]),
        )
    prediction_bytes_equal = array_equal_bytes(
        decode_array(full_inference.payload["final_prediction"]),
        decode_array(recovery_inference.payload["final_prediction"]),
    )
    return {
        "activation_bytes_equal": all(layer_bytes.values()),
        "prediction_bytes_equal": prediction_bytes_equal,
        "dequantized_bytes_equal": all(
            item["dequantized_bytes_equal"] for item in quant_bytes.values()
        ),
        "scale_bytes_equal": all(
            item["scale_bytes_equal"] for item in quant_bytes.values()
        ),
        "code_bytes_equal": all(
            item["code_bytes_equal"] for item in quant_bytes.values()
        ),
        "layer_activation_bytes": layer_bytes,
        "quant_tensor_bytes": quant_bytes,
    }


def run_demo(
    *,
    crash_after_layer: int = 1,
    artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    artifacts = Path(artifacts_dir or (PROJECT_ROOT / "artifacts")).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    full_dir = artifacts / "demo_full"
    recovery_dir = artifacts / "demo_recovery"
    full_state = PROJECT_ROOT / "runtime" / "full-state"
    recovery_state = PROJECT_ROOT / "runtime" / "recovery-state"
    full_cache = PROJECT_ROOT / "runtime" / "full-cache"
    recovery_cache = PROJECT_ROOT / "runtime" / "recovery-cache"
    full_tmp = PROJECT_ROOT / "runtime" / "full-tmp"
    recovery_tmp = PROJECT_ROOT / "runtime" / "recovery-tmp"
    common = ["--tolerance-abs", "0.25", "--result-file"]
    full_result_path = full_dir / "result.json"
    resumed_result_path = recovery_dir / "result.json"
    py = sys.executable
    full = _run_command(
        [
            py,
            "-m",
            "quantlab.cli",
            "run",
            "--clean",
            "--state-dir",
            str(full_state),
            "--cache-dir",
            str(full_cache),
            "--temp-dir",
            str(full_tmp),
            "--artifacts-dir",
            str(full_dir),
            *common,
            str(full_result_path),
        ]
    )
    if not full_result_path.exists():
        raise BusinessError(E_IO, "完整运行没有生成结果文件", stage="demo")
    full_result = _read_result(full_result_path)
    crash = _run_command(
        [
            py,
            "-m",
            "quantlab.cli",
            "crash-run",
            "--clean",
            "--state-dir",
            str(recovery_state),
            "--cache-dir",
            str(recovery_cache),
            "--temp-dir",
            str(recovery_tmp),
            "--artifacts-dir",
            str(recovery_dir / "crashed"),
            "--crash-after-layer",
            str(crash_after_layer),
        ],
        expect_exit=23,
    )
    resumed = _run_command(
        [
            py,
            "-m",
            "quantlab.cli",
            "resume",
            "--state-dir",
            str(recovery_state),
            "--cache-dir",
            str(recovery_cache),
            "--temp-dir",
            str(recovery_tmp),
            "--artifacts-dir",
            str(recovery_dir),
            "--result-file",
            str(resumed_result_path),
        ]
    )
    if not resumed_result_path.exists():
        raise BusinessError(E_IO, "恢复运行没有生成结果文件", stage="demo")
    resumed_result = _read_result(resumed_result_path)
    comparison = compare_full_and_resumed(full_result, resumed_result)
    tensor_comparison = compare_snapshot_tensors(
        full_state,
        full_tmp,
        recovery_state,
        recovery_tmp,
    )
    comparison["tensor_bytes"] = tensor_comparison
    comparison["all_equal"] = comparison["all_equal"] and all(
        tensor_comparison[key]
        for key in (
            "activation_bytes_equal",
            "prediction_bytes_equal",
            "dequantized_bytes_equal",
            "scale_bytes_equal",
            "code_bytes_equal",
        )
    )
    if not comparison["all_equal"]:
        raise BusinessError(
            E_RECOVERY_MISMATCH,
            "恢复结果与完整运行不一致",
            stage="demo",
            details=comparison,
        )
    report = {
        "schema": 1,
        "crash_after_layer": int(crash_after_layer),
        "crash_child": crash,
        "full_result": str(full_result_path),
        "resumed_result": str(resumed_result_path),
        "comparison": comparison,
        "full_summary": {
            "diagnostics": full_result["diagnostics"]["dominant_source"],
            "prediction": full_result["inference"]["final_prediction"],
            "latest_version": full_result["progress"]["latest"]["version"],
        },
        "resumed_summary": {
            "recovery_mode": resumed_result["recovery"]["mode"],
            "prediction": resumed_result["inference"]["final_prediction"],
            "latest_version": resumed_result["progress"]["latest"]["version"],
        },
    }
    atomic_write_json(
        artifacts / "demo_report.json",
        report,
        temp_dir=recovery_tmp,
        indent=2,
    )
    dashboard = build_dashboard(
        resumed_result,
        result_path=resumed_result_path,
        recovery_comparison=comparison,
    )
    dashboard["demo_report_path"] = str(artifacts / "demo_report.json")
    dashboard["comparison"] = comparison
    atomic_write_json(
        artifacts / "dashboard.json",
        dashboard,
        temp_dir=recovery_tmp,
        indent=2,
    )
    return {
        "ok": True,
        "comparison": comparison,
        "dashboard": str(artifacts / "dashboard.json"),
        "demo_report": str(artifacts / "demo_report.json"),
        "crash_child": crash,
    }
