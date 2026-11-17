"""Command-line interface for the local failover editing console."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .codes import BusinessCode
from .engine import FailoverEngine
from .errors import BusinessError
from .jsonl_logger import JsonlLogger
from .models import VALID_FAULT_KINDS, Scene
from .scene import SceneLoader


def _parse_pairs(values: list[str], *, kind: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        left, separator, right = value.partition("=")
        if not separator or not left or not right:
            raise BusinessError(
                BusinessCode.E_PROBE,
                f"无效 {kind} 参数 {value!r}，应为 KEY=VALUE",
                details={"value": value},
            )
        result[left] = right
    return result


def _parse_port_map(values: list[str]) -> dict[str, int]:
    pairs = _parse_pairs(values, kind="--port-map")
    result: dict[str, int] = {}
    for instance_id, text in pairs.items():
        try:
            port = int(text)
        except ValueError as exc:
            raise BusinessError(
                BusinessCode.E_SCENE,
                f"端口映射必须是整数: {instance_id}={text}",
                details={"instance_id": instance_id, "port": text},
            ) from exc
        if not 1 <= port <= 65535:
            raise BusinessError(
                BusinessCode.E_SCENE,
                f"端口超出 1 至 65535: {port}",
                details={"instance_id": instance_id, "port": port},
            )
        result[instance_id] = port
    return result


def _load_scene(args: argparse.Namespace) -> Scene:
    scene = SceneLoader().load(
        args.scene, inject_corruption=args.inject == "scene-corruption"
    )
    port_map = _parse_port_map(args.port_map)
    if port_map:
        try:
            scene = scene.with_port_overrides(port_map)
        except (KeyError, ValueError) as exc:
            raise BusinessError(
                BusinessCode.E_SCENE,
                str(exc),
                details={"port_map": port_map},
            ) from exc
    return scene


def _fault_overrides(args: argparse.Namespace, scene: Scene) -> dict[str, str]:
    overrides = _parse_pairs(getattr(args, "fault", []), kind="--fault")
    for instance_id, kind in overrides.items():
        if kind not in VALID_FAULT_KINDS:
            raise BusinessError(
                BusinessCode.E_PROBE,
                f"未知故障类型 {kind!r}，应为 unreachable/timeout/error",
                details={"instance_id": instance_id, "kind": kind},
            )
    if args.inject == "unreachable":
        instance_id = args.inject_instance
        if not instance_id:
            primary = sorted(
                (
                    instance
                    for instance in scene.instances()
                    if instance.role == "primary"
                ),
                key=lambda item: (item.service_id, item.instance_id),
            )
            if not primary:
                raise BusinessError(
                    BusinessCode.E_PROBE,
                    "没有可注入不可达的主实例",
                )
            instance_id = primary[0].instance_id
        overrides[instance_id] = "unreachable"
    return overrides


def _logger(args: argparse.Namespace) -> JsonlLogger:
    path = getattr(args, "log", None) or "artifacts/session_log.jsonl"
    return JsonlLogger(path, echo=False)


def _engine(args: argparse.Namespace, scene: Scene, logger: JsonlLogger) -> FailoverEngine:
    return FailoverEngine(
        scene,
        args.db,
        logger=logger,
        fail_snapshot_writes=args.inject == "snapshot-write-failure",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows 本地故障隔离与容灾切换编辑台（标准库实现）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, *, db_required: bool = False) -> None:
        sub.add_argument("--scene", required=True, help="本地场景 JSON 文件")
        sub.add_argument("--db", required=db_required, help="SQLite 会话库路径")
        sub.add_argument("--log", help="结构化 JSONL 日志路径")
        sub.add_argument(
            "--port-map",
            action="append",
            default=[],
            metavar="INSTANCE=PORT",
            help="覆盖场景实例的回环端口，可重复",
        )
        sub.add_argument(
            "--inject",
            choices=("scene-corruption", "unreachable", "snapshot-write-failure"),
            help="异常注入",
        )
        sub.add_argument("--inject-instance", help="--inject unreachable 的目标实例")

    validate = subparsers.add_parser("validate", help="严格校验场景")
    common(validate)

    ports = subparsers.add_parser("check-ports", help="检查回环端口占用")
    ports.add_argument("--scene", required=True)
    ports.add_argument("--port-map", action="append", default=[])

    status = subparsers.add_parser("status", help="查看恢复后的会话状态")
    common(status, db_required=True)

    restore = subparsers.add_parser("restore", help="从 SQLite 快照恢复")
    common(restore, db_required=True)

    replay = subparsers.add_parser("replay", help="重放最近快照并逐项比较")
    common(replay, db_required=True)

    cycle = subparsers.add_parser("run", aliases=["cycle"], help="执行一轮探测与切换")
    common(cycle, db_required=True)
    cycle.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="INSTANCE=KIND",
        help="本轮运行时故障覆盖，可重复",
    )
    cycle.add_argument("--now-epoch-ms", type=int, help="固定决策时钟，便于确定性比较")
    cycle.add_argument("--hold-after-snapshot", metavar="MARKER", help="快照后等待被强制终止")

    approve = subparsers.add_parser("approve", help="人工确认待切换请求")
    common(approve, db_required=True)
    approve.add_argument("--request-id", required=True)
    approve.add_argument(
        "--fault", action="append", default=[], metavar="INSTANCE=KIND"
    )
    approve.add_argument("--now-epoch-ms", type=int)

    gui = subparsers.add_parser("gui", help="启动 Tkinter 编辑台")
    gui.add_argument("--scene", help="初始场景路径")
    gui.add_argument("--db", help="初始会话库路径")
    gui.add_argument("--log", help="结构化日志路径")
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger: JsonlLogger | None = None
    try:
        if args.command == "gui":
            from .gui import main as gui_main

            return gui_main(scene_path=args.scene, db_path=args.db, log_path=args.log)
        if args.command == "check-ports":
            import check_ports

            port_args: list[str] = []
            for value in args.port_map:
                port_args.extend(["--port-map", value])
            return check_ports.main(["--scene", args.scene, *port_args])

        logger = _logger(args) if getattr(args, "scene", None) else None
        assert logger is not None
        scene = _load_scene(args)
        if args.inject == "unreachable":
            _fault_overrides(args, scene)
        logger.info(
            BusinessCode.OK.value,
            "场景已通过强校验",
            scene_version=scene.scene_version,
            scene_fingerprint=scene.fingerprint,
            service_count=len(scene.services),
            instance_count=len(scene.instances()),
        )
        if args.command == "validate":
            _emit(
                {
                    "ok": True,
                    "code": BusinessCode.OK.value,
                    "scene_version": scene.scene_version,
                    "fingerprint": scene.fingerprint,
                    "services": len(scene.services),
                    "instances": len(scene.instances()),
                }
            )
            return 0

        if args.inject == "scene-corruption":
            raise AssertionError("scene-corruption injection did not fail validation")

        with _engine(args, scene, logger) as engine:
            if args.command == "status":
                _emit({"ok": True, "code": BusinessCode.OK.value, **engine.status()})
                return 0
            if args.command == "restore":
                _emit(
                    {
                        "ok": True,
                        "code": BusinessCode.OK.value,
                        "recovery": engine.recovery.as_dict(),
                        "status": engine.status(),
                    }
                )
                return 0
            if args.command == "replay":
                result = engine.replay_latest()
                if not result.ok:
                    logger.error(
                        result.code,
                        "快照重放不一致",
                        snapshot_id=result.snapshot_id,
                        differences=[item.as_dict() for item in result.differences],
                    )
                _emit(result.as_dict())
                return 0 if result.ok else 2
            if args.command in {"run", "cycle"}:
                overrides = _fault_overrides(args, scene)
                result = engine.run_cycle(
                    runtime_faults=overrides,
                    now_epoch_ms_override=args.now_epoch_ms,
                )
                payload = {"ok": True, **result.as_dict()}
                if args.hold_after_snapshot:
                    marker = Path(args.hold_after_snapshot)
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    logger.info(
                        "SNAPSHOT_READY",
                        "快照已持久化，进程等待强制终止",
                        marker=str(marker),
                        snapshot_id=result.snapshot_id,
                    )
                    while True:
                        time.sleep(60)
                _emit(payload)
                return 0
            if args.command == "approve":
                overrides = _fault_overrides(args, scene)
                result = engine.approve_manual(
                    args.request_id,
                    runtime_faults=overrides,
                    now_epoch_ms_override=args.now_epoch_ms,
                )
                _emit({"ok": True, **result.as_dict()})
                return 0
        raise BusinessError(BusinessCode.E_SCENE, f"未实现的命令: {args.command}")
    except BusinessError as exc:
        if logger:
            logger.business_error(exc)
        _emit({"ok": False, **exc.as_dict()})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
