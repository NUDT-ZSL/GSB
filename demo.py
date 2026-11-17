"""Run the three required deterministic demonstration scenarios."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from failover_console.scene import SceneLoader

ROOT = Path(__file__).resolve().parent
DEMO_DIR = ROOT / "artifacts" / "demo"
EFFECTIVE_SCENE = DEMO_DIR / "effective_scene.json"
DB_PATH = DEMO_DIR / "session.sqlite"
LOG_PATH = DEMO_DIR / "session.jsonl"
MARKER_PATH = DEMO_DIR / "snapshot_ready.json"
READY_PATH = DEMO_DIR / "mock_ready.json"
RESULT_PATH = DEMO_DIR / "result.json"


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _ephemeral_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _prepare_scene() -> tuple[Path, dict[str, Any]]:
    source = ROOT / "scenes" / "sample_scene.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    ports = [
        item["port"]
        for service in data["services"]
        for item in service["instances"]
    ]
    if all(_port_free(port) for port in ports):
        return source, data

    candidates: list[int] = []
    while len(candidates) < len(ports):
        candidate = _ephemeral_port()
        if candidate >= 40000 and candidate not in candidates:
            candidates.append(candidate)
    candidate_iter = iter(candidates)
    for service in data["services"]:
        for instance in service["instances"]:
            instance["port"] = next(candidate_iter)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    EFFECTIVE_SCENE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return EFFECTIVE_SCENE, data


def _base_command(command: str, scene_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "failover_console",
        command,
        "--scene",
        str(scene_path),
        "--db",
        str(DB_PATH),
        "--log",
        str(LOG_PATH),
    ]


def _run_json(command: list[str], *, timeout: int = 40) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"命令未返回 JSON，exit={completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"命令失败 exit={completed.returncode}: {json.dumps(payload, ensure_ascii=False)}"
        )
    return payload


def _wait_mock(scene: dict[str, Any], process: subprocess.Popen[str]) -> None:
    deadline = time.time() + 20
    ports = [
        instance["port"]
        for service in scene["services"]
        for instance in service["instances"]
    ]
    while time.time() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise RuntimeError(f"模拟集群提前退出: stdout={stdout} stderr={stderr}")
        try:
            with socket.create_connection(("127.0.0.1", ports[0]), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("等待模拟集群启动超时")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _status(scene_path: Path) -> dict[str, Any]:
    return _run_json(_base_command("restore", scene_path))


def main() -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    for path in (DB_PATH, LOG_PATH, MARKER_PATH, READY_PATH, RESULT_PATH):
        _unlink(path)
    # SQLite WAL sidecars from an interrupted previous run are also removed.
    for suffix in ("-wal", "-shm"):
        _unlink(Path(str(DB_PATH) + suffix))

    scene_path, scene_data = _prepare_scene()
    # Validate before any subprocess starts.
    scene = SceneLoader().load(scene_path)
    mock_process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "mock_cluster.py",
            "--scene",
            str(scene_path),
            "--ready-file",
            str(READY_PATH),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    forced_kill_code: int | None = None
    try:
        _wait_mock(scene_data, mock_process)

        section1 = _run_json(
            _base_command("run", scene_path)
            + ["--fault", "payments-a=unreachable", "--now-epoch-ms", "1000000"]
        )
        section1_switches = [
            item
            for item in section1["switch_decisions"]
            if item["code"] == "SWITCHED"
        ]
        if not any(
            item["service_id"] == "payments" and item["target_instance"] == "payments-b"
            for item in section1_switches
        ):
            raise RuntimeError(f"第一段未按确定目标切换: {section1_switches}")
        if section1["probe_batch"]["peak_global"] > scene.probe_limits.global_concurrency:
            raise RuntimeError("全局并发预算被突破")
        if any(
            peak > scene.probe_limits.service_quota
            for peak in section1["probe_batch"]["peak_by_service"].values()
        ):
            raise RuntimeError("单服务探测配额被突破")

        status1 = _status(scene_path)
        primary1 = {
            item["service_id"]: item["instance_id"]
            for item in status1["status"]["instances"]
            if item["role"] == "primary"
        }
        if primary1.get("payments") != "payments-b":
            raise RuntimeError(f"第一段切换后主实例错误: {primary1}")

        section2 = _run_json(
            _base_command("run", scene_path)
            + ["--fault", "payments-b=unreachable", "--now-epoch-ms", "1000100"]
        )
        if "E_COOLDOWN" not in section2["codes"]:
            raise RuntimeError(f"第二段缺少 E_COOLDOWN: {section2['codes']}")
        cooldown_decisions = [
            item for item in section2["switch_decisions"] if item["code"] == "E_COOLDOWN"
        ]
        if not cooldown_decisions or cooldown_decisions[0]["target_instance"] != "payments-c":
            raise RuntimeError(f"第二段冷却决策目标异常: {cooldown_decisions}")
        status2 = _status(scene_path)
        primary2 = {
            item["service_id"]: item["instance_id"]
            for item in status2["status"]["instances"]
            if item["role"] == "primary"
        }
        if primary2.get("payments") != "payments-b":
            raise RuntimeError(f"冷却期间角色被错误推进: {primary2}")

        crash_command = _base_command("run", scene_path) + [
            "--fault",
            "payments-b=unreachable",
            "--now-epoch-ms",
            "1200000",
            "--hold-after-snapshot",
            str(MARKER_PATH),
        ]
        crash_process = subprocess.Popen(
            crash_command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if MARKER_PATH.exists():
                break
            if crash_process.poll() is not None:
                stdout, stderr = crash_process.communicate(timeout=2)
                raise RuntimeError(
                    f"第三段进程在快照前退出: exit={crash_process.returncode}\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            time.sleep(0.05)
        else:
            crash_process.kill()
            raise RuntimeError("等待第三段快照落盘超时")
        crash_process.kill()
        crash_stdout, crash_stderr = crash_process.communicate(timeout=10)
        forced_kill_code = int(crash_process.returncode) if crash_process.returncode is not None else None
        marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        if marker.get("snapshot_id") is None:
            raise RuntimeError("第三段快照未持久化")

        restored = _status(scene_path)
        primary3 = {
            item["service_id"]: item["instance_id"]
            for item in restored["status"]["instances"]
            if item["role"] == "primary"
        }
        if primary3.get("payments") != "payments-c":
            raise RuntimeError(f"强制终止恢复后主实例错误: {primary3}")
        if not any(
            item["instance_id"] == "payments-b" and item["isolated"]
            for item in restored["status"]["instances"]
        ):
            raise RuntimeError("被替换的 payments-b 未保持隔离")

        replay = _run_json(_base_command("replay", scene_path))
        if replay.get("code") != "OK_REPLAY":
            raise RuntimeError(f"重放不一致: {json.dumps(replay, ensure_ascii=False)}")

        with sqlite3.connect(DB_PATH) as connection:
            snapshot_count = int(
                connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )
            completed_switches = int(
                connection.execute(
                    "SELECT COUNT(*) FROM switch_decisions WHERE switched=1"
                ).fetchone()[0]
            )
            running_switches = int(
                connection.execute("SELECT COUNT(*) FROM switch_transactions").fetchone()[0]
            )
        result = {
            "ok": True,
            "scene": str(scene_path.relative_to(ROOT)) if scene_path.is_relative_to(ROOT) else str(scene_path),
            "scene_version": scene.scene_version,
            "ports": {
                item.instance_id: item.port for item in scene.instances()
            },
            "section1_full_probe_and_switch": {
                "codes": section1["codes"],
                "target": "payments-b",
                "primary_after": primary1,
                "peak_global": section1["probe_batch"]["peak_global"],
                "global_limit": scene.probe_limits.global_concurrency,
                "peak_by_service": section1["probe_batch"]["peak_by_service"],
                "service_quota": scene.probe_limits.service_quota,
            },
            "section2_cooldown_suppression": {
                "codes": section2["codes"],
                "cooldown_target": cooldown_decisions[0]["target_instance"],
                "remaining_ms": cooldown_decisions[0]["details"].get("remaining_ms"),
                "primary_after": primary2,
            },
            "section3_forced_kill_restore_replay": {
                "forced_kill_returncode": forced_kill_code,
                "snapshot_id": marker["snapshot_id"],
                "recovery": restored["recovery"],
                "primary_after": primary3,
                "replay": replay["code"],
                "snapshot_count": snapshot_count,
                "completed_switches": completed_switches,
                "running_switches_after_recovery": running_switches,
                "crash_stdout_tail": crash_stdout[-500:],
                "crash_stderr_tail": crash_stderr[-500:],
            },
        }
        RESULT_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if mock_process.poll() is None:
            mock_process.terminate()
            try:
                mock_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                mock_process.kill()
                mock_process.communicate(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
