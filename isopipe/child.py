from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isopipe.canonical import canonical_bytes, sha256_bytes  # noqa: E402
from isopipe.kernel import serial_aggregate  # noqa: E402
from isopipe.manifest import TaskSpec  # noqa: E402


LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14
ACCESS_IOCTL_DEV = 1 << 15

READ_ACCESS = ACCESS_READ_FILE | ACCESS_READ_DIR
WRITE_ACCESS = (
    ACCESS_WRITE_FILE
    | ACCESS_READ_FILE
    | ACCESS_READ_DIR
    | ACCESS_REMOVE_DIR
    | ACCESS_REMOVE_FILE
    | ACCESS_MAKE_CHAR
    | ACCESS_MAKE_DIR
    | ACCESS_MAKE_REG
    | ACCESS_MAKE_SOCK
    | ACCESS_MAKE_FIFO
    | ACCESS_MAKE_BLOCK
    | ACCESS_MAKE_SYM
    | ACCESS_REFER
    | ACCESS_TRUNCATE
)
ALL_ACCESS = READ_ACCESS | WRITE_ACCESS | ACCESS_EXECUTE | ACCESS_IOCTL_DEV


class SandboxDenied(PermissionError):
    def __init__(self, kind: str, path: str) -> None:
        super().__init__(f"sandbox denied {kind}: {path}")
        self.kind = kind
        self.path = path


def _write_envelope(path: Path, value: dict[str, Any]) -> None:
    try:
        path.write_bytes(canonical_bytes(value) + b"\n")
    except OSError:
        pass


def _inside(path: Path, scopes: list[Path]) -> bool:
    for scope in scopes:
        try:
            path.relative_to(scope)
            return True
        except ValueError:
            continue
    return False


def _install_audit_guard(read_scopes: list[Path], write_scopes: list[Path]) -> None:
    def guard(event: str, args: tuple[Any, ...]) -> None:
        if not args:
            return
        if event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown"):
            try:
                path = Path(os.path.abspath(os.fspath(args[0])))
            except (TypeError, OSError, ValueError):
                return
            if not _inside(path, write_scopes):
                raise SandboxDenied("write", str(path))
            return
        if event in ("os.rename", "os.replace", "os.link", "os.symlink"):
            paths = args[:2] if event != "os.symlink" else args[1:2]
            for raw_path in paths:
                try:
                    path = Path(os.path.abspath(os.fspath(raw_path)))
                except (TypeError, OSError, ValueError):
                    continue
                if not _inside(path, write_scopes):
                    raise SandboxDenied("write", str(path))
            return
        if event != "open":
            return
        raw_path = args[0]
        if isinstance(raw_path, int) or raw_path is None:
            return
        try:
            path = Path(os.path.abspath(os.fspath(raw_path)))
        except (TypeError, OSError, ValueError):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = False
        if isinstance(mode, str):
            writing = any(char in mode for char in "wax+")
        if isinstance(flags, int):
            writing = writing or bool(
                flags
                & (
                    os.O_WRONLY
                    | os.O_RDWR
                    | os.O_CREAT
                    | os.O_TRUNC
                    | getattr(os, "O_APPEND", 0)
                )
            )
        if writing and not _inside(path, write_scopes):
            raise SandboxDenied("write", str(path))
        if not writing and not (_inside(path, read_scopes) or _inside(path, write_scopes)):
            raise SandboxDenied("read", str(path))

    sys.addaudithook(guard)


def _set_parent_death_signal() -> None:
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, 9, 0, 0, 0)


def _landlock_syscall_numbers() -> tuple[int, int, int]:
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        return 444, 445, 446
    if machine in ("aarch64", "arm64"):
        return 444, 445, 446
    raise OSError(f"Landlock syscall numbers are unknown for {machine}")


def _apply_landlock(read_scopes: list[Path], write_scopes: list[Path]) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError("Landlock is only available on Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    create_nr, add_nr, restrict_nr = _landlock_syscall_numbers()
    abi = libc.syscall(
        create_nr, ctypes.c_void_p(0), ctypes.c_size_t(0), LANDLOCK_CREATE_RULESET_VERSION
    )
    if abi < 1:
        errno = ctypes.get_errno()
        raise OSError(errno, f"Landlock unavailable: {os.strerror(errno)}")
    handled = ALL_ACCESS
    if abi < 3:
        handled &= ~ACCESS_TRUNCATE
    if abi < 2:
        handled &= ~ACCESS_REFER
    if abi < 5:
        handled &= ~ACCESS_IOCTL_DEV
    attr = ctypes.c_uint64(handled)
    ruleset_fd = libc.syscall(
        create_nr,
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"landlock_create_ruleset failed: {os.strerror(errno)}")

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("allowed_access", ctypes.c_uint64),
            ("parent_fd", ctypes.c_int32),
        ]

    def add_rule(path: Path, access: int) -> None:
        path = path.resolve(strict=False)
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        if path.is_dir():
            effective_access = access
        elif access & ACCESS_WRITE_FILE:
            effective_access = (
                ACCESS_WRITE_FILE
                | ACCESS_READ_FILE
                | ACCESS_TRUNCATE
                | ACCESS_REFER
            )
        else:
            effective_access = ACCESS_READ_FILE
        try:
            rule = PathBeneath(allowed_access=effective_access, parent_fd=fd)
            result = libc.syscall(
                add_nr,
                ctypes.c_int(ruleset_fd),
                ctypes.c_uint32(LANDLOCK_RULE_PATH_BENEATH),
                ctypes.byref(rule),
                ctypes.c_uint32(0),
            )
            if result < 0:
                errno = ctypes.get_errno()
                raise OSError(errno, f"landlock_add_rule failed: {os.strerror(errno)}")
        finally:
            os.close(fd)

    try:
        for scope in read_scopes:
            add_rule(scope, READ_ACCESS)
        for scope in write_scopes:
            add_rule(scope, WRITE_ACCESS)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(errno)}")
        if libc.syscall(restrict_nr, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0)) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"landlock_restrict_self failed: {os.strerror(errno)}")
    finally:
        os.close(ruleset_fd)


def _denied_error(exc: BaseException) -> dict[str, Any] | None:
    if isinstance(exc, SandboxDenied):
        code = (
            "E_SANDBOX_READ_DENIED"
            if exc.kind == "read"
            else "E_SANDBOX_WRITE_DENIED"
        )
        return {
            "ok": False,
            "error_code": code,
            "message": f"sandbox denied {exc.kind} access",
            "path": exc.path,
        }
    if isinstance(exc, PermissionError):
        filename = os.fspath(exc.filename) if exc.filename else ""
        flags = getattr(exc, "errno", None)
        code = (
            "E_SANDBOX_WRITE_DENIED"
            if flags in (13, 30) and getattr(exc, "filename2", None)
            else "E_SANDBOX_READ_DENIED"
        )
        return {
            "ok": False,
            "error_code": code,
            "message": "sandbox denied filesystem access",
            "path": filename,
        }
    return None


def main() -> int:
    _set_parent_death_signal()
    payload = json.load(sys.stdin)
    envelope_path = Path(payload["envelope_path"])
    staging_path = Path(payload["staging_path"])
    workdir = Path(payload["workdir"])
    read_scopes = [Path(item).resolve(strict=False) for item in payload["read_scopes"]]
    write_scopes = [Path(item).resolve(strict=False) for item in payload["write_scopes"]]
    output_path = Path(payload["output_path"])
    fixed_scale = int(payload["fixed_scale"])
    kernel_version = str(payload["kernel_version"])
    task_data = payload["task"]
    task = TaskSpec(
        name=task_data["name"],
        inputs=tuple(task_data.get("inputs", [])),
        depends_on=tuple(task_data.get("depends_on", [])),
        reads=tuple(task_data.get("reads", [])),
        writes=tuple(task_data.get("writes", [])),
        env_allowlist=tuple(task_data.get("env_allowlist", [])),
        timeout_ms=int(task_data["timeout_ms"]),
        logical_cost_ms=int(task_data["logical_cost_ms"]),
        output=task_data["output"],
        workdir=task_data["workdir"],
        operation=task_data.get("operation", "aggregate"),
        probe_path=task_data.get("probe_path"),
        compute_delay_ms=int(task_data.get("compute_delay_ms", 0)),
    )
    try:
        os.chdir(workdir)
        try:
            _apply_landlock(read_scopes, write_scopes)
        except OSError as exc:
            _write_envelope(
                envelope_path,
                {
                    "ok": False,
                    "error_code": "E_SANDBOX_UNAVAILABLE",
                    "message": f"kernel sandbox is unavailable: {exc}",
                },
            )
            return 1
        _install_audit_guard(read_scopes, write_scopes)
        if task.operation == "probe_read":
            target = (Path(payload["root"]) / task.probe_path).resolve(strict=False)
            with target.open("rb") as handle:
                handle.read(1)
            raise RuntimeError("probe read unexpectedly succeeded")
        if task.operation == "probe_write":
            target = (Path(payload["root"]) / task.probe_path).resolve(strict=False)
            with target.open("wb") as handle:
                handle.write(b"escape-probe")
            raise RuntimeError("probe write unexpectedly succeeded")

        if task.compute_delay_ms:
            time.sleep(task.compute_delay_ms / 1000.0)
        environment = payload.get("environment", {})
        result = serial_aggregate(
            task,
            [Path(item) for item in payload["input_paths"]],
            Path(payload["root"]),
            fixed_scale=fixed_scale,
            environment=environment,
            kernel_version=kernel_version,
        )
        result_file = staging_path
        result_file.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_bytes(result) + b"\n"
        temp = result_file.with_name(f".{result_file.name}.tmp-{os.getpid()}")
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, result_file)
        _write_envelope(
            envelope_path,
            {
                "ok": True,
                "result_digest": sha256_bytes(canonical_bytes(result)),
                "staging_path": str(staging_path),
                "output_path": str(output_path),
                "stdout": "",
                "stderr": "",
            },
        )
        return 0
    except BaseException as exc:
        denied = _denied_error(exc)
        if denied is not None:
            _write_envelope(envelope_path, denied)
            return 3
        _write_envelope(
            envelope_path,
            {
                "ok": False,
                "error_code": getattr(exc, "code", "E_INTERNAL"),
                "message": str(exc),
                "traceback": traceback.format_exc(limit=5),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
