# -*- coding: utf-8 -*-
"""隔离沙箱模拟利用器。

工作方式（任务要求 3/4/8/10）：
- 每条静态漏洞在独立的临时沙箱中模拟一次上传；
- 沙箱内含 uploads/ 上传根目录与同级“隔离目录外”区域；
- 按配置构造含 ``../`` 的恶意文件名，调用样例服务自身的保存入口函数；
- 若标记文件出现在上传根目录之外且内容与指定内容逐字节一致 -> exploitable=true；
- 目标已存在同名文件时保留原文件并记录 conflict；
- 只读目录模式下输出明确权限错误并以非零退出码退出（由 CLI 层处理退出码）。
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import rules as R
from .detector import Finding

DEFAULT_MARKER_NAME = "pwned_marker.txt"
DEFAULT_PAYLOAD = "../" + DEFAULT_MARKER_NAME
DEFAULT_CONTENT = b"PWNED-MARKER-27857 path traversal PoC\n"

# 读取/删除类汇聚点（无写入行为，模拟时预置目标文件验证越界访问）
READ_SINK_NAMES = {"Path.read_text", "Path.read_bytes", "read"}
DELETE_SINK_NAMES = {"os.remove", "os.unlink", "Path.unlink", "shutil.rmtree"}


@dataclass
class SimConfig:
    marker_name: str = DEFAULT_MARKER_NAME
    payload: str = DEFAULT_PAYLOAD            # 含 ../ 的恶意文件名
    content: bytes = DEFAULT_CONTENT
    entry_candidates: tuple[str, ...] = (
        "save_upload", "upload_file", "handle_upload",
    )
    precreate_conflict: bool = False          # 是否预置同名文件（任务4）
    read_only: bool = False                   # 只读目录模式（任务10）
    adaptive_payload: bool = True             # 按 sink 类型自适应 payload


@dataclass
class SimResult:
    finding_id: str
    file: str
    line: int
    severity: str
    entry_function: str | None
    payload: str
    sandbox: str
    target_path: str | None
    outside_upload_root: bool
    exploitable: bool
    content_matches: bool
    conflict: dict | None
    error: str | None = None
    mode: str = "write"


def _is_read_sink(finding) -> bool:
    return finding.sink in READ_SINK_NAMES


def _is_delete_sink(finding) -> bool:
    return finding.sink in DELETE_SINK_NAMES


def _adaptive_payload(finding, cfg: SimConfig) -> tuple[str, str]:
    """按漏洞形态返回 (payload, mode)。

    - 弱净化（中危 replace 黑名单）：....// 经一次替换还原为 ../
    - 扩展名白名单：标记文件追加 .png 后缀
    - 读取/删除类：沿用 ../ 逃逸，mode 改为 read/delete
    """
    marker = cfg.marker_name
    if _is_read_sink(finding):
        return cfg.payload, "read"
    if _is_delete_sink(finding):
        return cfg.payload, "delete"
    if finding.taint_level == 1:  # WEAK：黑名单替换绕过（....// -> ../）
        return _WEAK_BYPASS + marker, "write"
    if "extension" in finding.file or "extension" in finding.code.lower():
        return "../" + marker + ".png", "write"
    return cfg.payload, "write"


# ---------------------------------------------------------------------------
# 模块加载（唯一命名，避免污染 sys.modules）
# ---------------------------------------------------------------------------
def _load_module(file_path: str):
    spec = importlib.util.spec_from_file_location(
        f"sim_target_{uuid.uuid4().hex}", file_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _resolve_entry(module, cfg: SimConfig):
    for name in cfg.entry_candidates:
        fn = getattr(module, name, None)
        if callable(fn):
            return name, fn
    # 兜底：模块内第一个以 save/upload 开头的可调用对象
    for name in dir(module):
        if name.startswith(("save", "upload")):
            fn = getattr(module, name)
            if callable(fn):
                return name, fn
    return None, None


def _call_entry(fn, upload_dir: Path, payload: str, content: bytes) -> object:
    """按入口函数签名自适应传参（upload_dir / filename / content 等常见形参）。"""
    sig = inspect.signature(fn)
    params = list(sig.parameters)
    kwargs: dict[str, object] = {}
    for p in params:
        low = p.lower()
        if low in {"upload_dir", "upload_root", "upload_path", "root", "dir", "directory", "base_dir"}:
            kwargs[p] = str(upload_dir)
        elif low in {"filename", "name", "fname", "file_name"}:
            kwargs[p] = payload
        elif low in {"content", "data", "payload", "body"}:
            kwargs[p] = content
        elif low in {"mode", "flag", "flags"}:
            kwargs[p] = "wb"
    try:
        return fn(**kwargs)
    except TypeError:
        # 签名不匹配时退化为位置参数 (upload_dir, filename, content)
        return fn(str(upload_dir), payload, content)


# ---------------------------------------------------------------------------
# 目标逃逸路径推算
# ---------------------------------------------------------------------------
_TRAVERSAL_RE = re.compile(r"\.\.[/\\]")
# 弱净化黑名单绕过形态：....// 或 ....\\（一次 replace("../","") 后还原为 ../ 或 ..\\）
_BYPASS_RE = re.compile(r"\.{4}([/\\])\1")
# 弱净化绕过 payload：固定使用正斜杠形态（样例替换的字面量为 "../"）
_WEAK_BYPASS = "....//"


def _escape_target(upload_dir: Path, payload: str, marker_name: str) -> Path:
    """根据 payload 推算标记文件落盘位置。

    单遍规范化：每个 ``....//``（弱净化绕过）与每个 ``../`` 各折算一层上跳，
    统一兼容正斜杠与 Windows 反斜杠形态。
    """
    depth = 0
    remainder = payload
    # 先摘走弱净化绕过片段，避免与普通 ../ 片段重复计数
    while True:
        m = _BYPASS_RE.search(remainder)
        if not m:
            break
        depth += 1
        remainder = remainder[:m.start()] + remainder[m.end():]
    depth += len(_TRAVERSAL_RE.findall(remainder))
    cleaned = _TRAVERSAL_RE.sub("", remainder)
    tail = cleaned or marker_name
    base = upload_dir
    for _ in range(depth):
        base = base.parent
    return base / tail


# ---------------------------------------------------------------------------
# 只读目录处理（POSIX chmod / Windows icacls 拒绝写入）
# ---------------------------------------------------------------------------
def make_readonly(path: Path) -> callable:
    """把目录设为只读，返回恢复函数。"""
    platform = sys.platform
    if platform.startswith("win"):
        import subprocess
        deny_cmd = ["icacls", str(path), "/deny", "*S-1-5-32-545:(WD)"]
        subprocess.run(deny_cmd, capture_output=True, check=False)
        deny_users = ["Users", "Authenticated Users"]
        for who in deny_users:
            subprocess.run(["icacls", str(path), "/deny", f"*{who}:(WD)"],
                           capture_output=True, check=False)

        def restore():
            subprocess.run(["icacls", str(path), "/reset", "/T", "/Q"],
                           capture_output=True, check=False)
            try:
                os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
            except OSError:
                pass
        return restore

    prev = stat.S_IMODE(os.stat(path).st_mode)
    os.chmod(path, prev & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)

    def restore():
        os.chmod(path, prev)
    return restore


def probe_writable(path: Path) -> bool:
    """写探针：在目录内尝试创建临时文件，失败即不可写。"""
    probe = path / f".write_probe_{uuid.uuid4().hex}"
    try:
        with open(probe, "wb") as fh:
            fh.write(b"probe")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 单条漏洞模拟
# ---------------------------------------------------------------------------
def simulate_one(source_root: str, finding: Finding, cfg: SimConfig,
                 sandbox_base: str | None = None) -> SimResult:
    source_path = os.path.join(source_root, finding.file)
    sandbox = Path(tempfile.mkdtemp(prefix="pt_sandbox_", dir=sandbox_base))
    upload_dir = sandbox / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 按漏洞形态自适应选择 payload 与模拟模式
    if cfg.adaptive_payload:
        payload, mode = _adaptive_payload(finding, cfg)
    else:
        payload, mode = cfg.payload, ("read" if _is_read_sink(finding) else "write")

    target = _escape_target(upload_dir, payload, cfg.marker_name)
    result = SimResult(
        finding_id=finding.id,
        file=finding.file,
        line=finding.line,
        severity=finding.severity,
        entry_function=None,
        payload=payload,
        sandbox=str(sandbox),
        target_path=str(target),
        outside_upload_root=False,
        exploitable=False,
        content_matches=False,
        conflict=None,
        mode=mode,
    )

    # 只读模式：先冻结上传目录，再做写探针（任务10）
    if cfg.read_only and mode == "write":
        restore = make_readonly(upload_dir)
        if not probe_writable(upload_dir):
            result.error = (
                f"权限不足：上传目录 {upload_dir} 为只读，无法写入文件"
                f"（PermissionError/WinError 拒绝访问）。模拟已中止，未执行写入。"
            )
            restore()
            return result
        restore()

    # 读取类：在逃逸目标处预置标记文件，验证服务是否能越界读到上传根之外
    if mode == "read":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cfg.content)

    # 冲突预置（任务4）：在目标位置预置同名文件与特征内容
    conflict_original = b"ORIGINAL-CONTENT-MUST-BE-PRESERVED\n"
    if cfg.precreate_conflict and mode == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(conflict_original)
        result.conflict = {
            "target": str(target),
            "original_content": conflict_original.decode(),
            "action": "preserved",
            "overwritten": False,
        }

    try:
        module = _load_module(source_path)
    except Exception as exc:  # 样例自身异常不应使工具崩溃
        result.error = f"加载目标模块失败: {type(exc).__name__}: {exc}"
        _restore_conflict(result, target, conflict_original)
        return result

    entry_name, entry_fn = _resolve_entry(module, cfg)
    result.entry_function = entry_name
    if entry_fn is None:
        result.error = "未找到上传入口函数（save_upload/upload_file/handle_upload 之一）"
        _restore_conflict(result, target, conflict_original)
        return result

    # 同名文件冲突（任务4）：目标已存在时保留原文件，跳过写入并记录冲突
    skipped_due_to_conflict = False
    if cfg.precreate_conflict and mode == "write" and target.exists():
        skipped_due_to_conflict = True
        result.error = None
        result.conflict = result.conflict or {
            "target": str(target),
            "original_content": conflict_original.decode(),
            "action": "preserved",
            "overwritten": False,
        }
        result.conflict["action"] = "skipped_write_to_preserve_existing_file"

    call_error = None
    returned = None
    if not skipped_due_to_conflict:
        try:
            returned = _call_entry(entry_fn, upload_dir, payload, cfg.content)
        except OSError as exc:
            call_error = f"入口函数抛出 {type(exc).__name__}: {exc}"
        except Exception as exc:
            call_error = f"入口函数抛出 {type(exc).__name__}: {exc}"
    result.error = call_error

    # 判定：标记文件是否逃逸到上传根目录之外
    try:
        target_resolved = target.resolve()
        root_resolved = upload_dir.resolve()
        result.outside_upload_root = _is_outside(target_resolved, root_resolved)

        if skipped_due_to_conflict:
            # 原文件内容必须逐字节保持不变；不标记可利用（写入被安全跳过）
            preserved = target.read_bytes() == conflict_original
            result.content_matches = False
            result.exploitable = False
            result.conflict["content_preserved"] = preserved
            return result

        if mode == "read":
            # 读取类：入口未抛错且返回内容与预置标记逐字节一致 -> 越界读取成立
            result.content_matches = returned == cfg.content
            result.exploitable = bool(call_error is None and result.content_matches
                                      and result.outside_upload_root)
        elif mode == "delete":
            result.exploitable = bool(call_error is None and not target.exists()
                                      and result.outside_upload_root)
        else:
            exists = target.exists()
            if exists:
                written = target.read_bytes()
                result.content_matches = written == cfg.content
            result.exploitable = bool(exists and result.outside_upload_root
                                      and result.content_matches)
    except OSError as exc:
        result.error = (result.error or "") + f" | 结果校验失败: {exc}"

    return result


def _is_outside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return False
    except ValueError:
        return True


def _restore_conflict(result: SimResult, target: Path, original: bytes) -> None:
    if result.conflict is not None and target.exists():
        if target.read_bytes() != original:
            target.write_bytes(original)
            result.conflict["overwritten"] = True
            result.conflict["action"] = "restored_after_unexpected_overwrite"


# ---------------------------------------------------------------------------
# 批量模拟
# ---------------------------------------------------------------------------
def simulate_all(source_root: str, findings: list[Finding],
                 cfg: SimConfig | None = None,
                 sandbox_base: str | None = None) -> list[SimResult]:
    cfg = cfg or SimConfig()
    results: list[SimResult] = []
    for finding in findings:
        results.append(simulate_one(source_root, finding, cfg, sandbox_base))
    return results


def result_to_dict(r: SimResult) -> dict:
    return {
        "finding_id": r.finding_id,
        "file": r.file,
        "line": r.line,
        "severity": r.severity,
        "entry_function": r.entry_function,
        "payload": r.payload,
        "sandbox": r.sandbox,
        "target_path": r.target_path,
        "outside_upload_root": r.outside_upload_root,
        "exploitable": r.exploitable,
        "content_matches": r.content_matches,
        "conflict": r.conflict,
        "error": r.error,
        "mode": r.mode,
    }
