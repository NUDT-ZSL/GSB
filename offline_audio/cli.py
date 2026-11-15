"""JSON-first command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import InterruptToken, prepare_job, run_job
from .errors import BusinessError, TaskCancelled
from .ffmpeg_tools import resolve_toolchain


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BusinessError(
            "INVALID_ARGUMENTS", "命令行参数无效", {"reason": message}
        )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="transcoder",
        description="离线确定性多轨 WAV 转码引擎（JSON 输出）",
    )
    parser.add_argument("manifest_pos", nargs="?", help="轨道清单 JSON")
    parser.add_argument("--manifest", dest="manifest", help="轨道清单 JSON")
    parser.add_argument("--output-spec", help="可选：独立输出规格 JSON")
    parser.add_argument("--output", help="覆盖成品 WAV 路径")
    parser.add_argument("--report", help="覆盖响度报告路径")
    parser.add_argument("--state", help="覆盖状态文件路径")
    parser.add_argument("--work-dir", help="覆盖工作/缓存目录")
    parser.add_argument("--concurrency", type=int, help="覆盖并发轨道数")
    parser.add_argument("--memory-limit-mb", type=int, help="覆盖内存预算（MiB）")
    parser.add_argument("--window-ms", type=float, help="覆盖统计窗口毫秒数")
    parser.add_argument("--force-restart", action="store_true", help="忽略旧状态从头执行")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", help="ffprobe 可执行文件路径")
    return parser


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"非标准 JSON 数值: {value}")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle, parse_constant=_reject_json_constant)
    except FileNotFoundError as exc:
        raise BusinessError(
            "INVALID_ARGUMENTS",
            f"{label}不存在",
            {"file": str(path), "kind": label},
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise BusinessError(
            "INVALID_JSON",
            f"无法解析{label}",
            {"file": str(path), "kind": label, "reason": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise BusinessError(
            "INVALID_MANIFEST",
            f"{label}根节点必须是 JSON 对象",
            {"file": str(path), "kind": label},
        )
    return payload


def _load_output_spec(path: Path, manifest: dict[str, Any]) -> None:
    spec = _read_json_object(path, "输出规格文件")
    data = spec.get("output", spec)
    if not isinstance(data, dict):
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出规格 output 必须是 JSON 对象",
            {"file": str(path)},
        )
    data = dict(data)
    base = path.parent
    for key in (
        "file",
        "path",
        "output_file",
        "output_path",
        "report_file",
        "report_path",
        "state_file",
        "state_path",
        "work_dir",
        "workDir",
    ):
        value = data.get(key)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            data[key] = str((base / value).resolve())
    manifest["output"] = data
    for key in ("report_file", "report_path", "state_file", "state_path", "work_dir"):
        value = spec.get(key)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            manifest[key] = str((base / value).resolve())


def _emit(payload: dict[str, Any]) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _interrupted(token: InterruptToken) -> BusinessError:
    return BusinessError(
        "INTERRUPTED",
        "任务收到中断信号",
        {"signal": token.signal_name, "signal_number": token.signal_number},
    )


def main(argv: Sequence[str] | None = None) -> int:
    token = InterruptToken()
    token.install()
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        manifest_value = args.manifest or args.manifest_pos
        if not manifest_value:
            raise BusinessError(
                "INVALID_ARGUMENTS",
                "必须提供清单路径（位置参数或 --manifest）",
                {"expected": "transcoder.py MANIFEST.json"},
            )
        manifest_path = Path(manifest_value).resolve()
        raw = _read_json_object(manifest_path, "清单文件")
        if args.output_spec:
            _load_output_spec(Path(args.output_spec).resolve(), raw)
        token.check()
        toolchain = resolve_toolchain(args.ffmpeg, args.ffprobe)
        job = prepare_job(
            raw,
            manifest_path,
            toolchain,
            output_override=args.output,
            report_override=args.report,
            state_override=args.state,
            work_override=args.work_dir,
            concurrency_override=args.concurrency,
            memory_override=args.memory_limit_mb,
            window_override=args.window_ms,
        )
        token.check()
        result = run_job(
            job,
            toolchain,
            token=token,
            force_restart=args.force_restart,
        )
        _emit(result)
        return 0
    except BusinessError as exc:
        if token.signal_number is not None and exc.code != "INTERRUPTED":
            interrupted = _interrupted(token)
            _emit(interrupted.to_dict())
            return interrupted.exit_code
        _emit(exc.to_dict())
        return exc.exit_code
    except TaskCancelled:
        interrupted = _interrupted(token)
        _emit(interrupted.to_dict())
        return interrupted.exit_code
    except KeyboardInterrupt:
        interrupted = _interrupted(token)
        _emit(interrupted.to_dict())
        return interrupted.exit_code
    except SystemExit:
        raise
    except BaseException as exc:
        if token.signal_number is not None:
            interrupted = _interrupted(token)
            _emit(interrupted.to_dict())
            return interrupted.exit_code
        error = BusinessError(
            "INTERNAL_ERROR",
            "命令行执行发生未预期错误",
            {"reason": repr(exc)},
        )
        _emit(error.to_dict())
        return error.exit_code
    finally:
        token.restore()


if __name__ == "__main__":
    raise SystemExit(main())
