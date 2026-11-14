#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 目录文件权限基线核查工具（仅使用 Python 标准库）。

用法:
    python file_audit.py <目录路径> <规则文件.json>

规则文件为 JSON 数组，每项形如:
    {"identity": "S-1-5-32-545", "access": "write"}
identity 支持 SID 字符串（S-1-...）或账户名（"user" / "DOMAIN\\user"，
匹配时大小写不敏感）；access 仅允许 "read" 或 "write"。

报告以 JSON 输出到标准输出:
    {
      "scan_root": "<绝对路径>",
      "risks":    [ {"file": ..., "identity": ..., "access": ...}, ... ],
      "unchecked": [ {"file": ..., "reason": ...}, ... ]
    }

退出码:
    0  扫描完成（即使存在未能核查的文件）
    2  参数/规则/目录等前置输入错误，且不产生报告
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import stat
import sys
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# Windows 常量
# --------------------------------------------------------------------------- #
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004

# CreateFileW 相关
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_SHARE_ALL = 0x00000007  # FILE_SHARE_READ|WRITE|DELETE，尽量不干扰业务进程
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
ERROR_ACCESS_DENIED = 5

ACL_SIZE_INFORMATION_CLASS = 2

ACCESS_ALLOWED_ACE_TYPE = 0x0

# 数据面访问位（不含 READ_CONTROL / SYNCHRONIZE / WRITE_DAC / WRITE_OWNER 等管理位）
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000

READ_BITS = FILE_READ_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES
WRITE_BITS = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
)

_KNOWN_WINERRORS = {
    2: "文件不存在或在扫描过程中被移除",
    3: "路径不存在或不可达",
    5: "拒绝访问：当前身份无权读取该文件的安全描述符",
    32: "共享冲突：文件正被其他进程独占占用，暂时无法读取访问控制项",
    33: "锁定冲突：文件已被其他进程锁定，暂时无法读取访问控制项",
    123: "路径语法无效",
}


class RuleError(ValueError):
    """规则文件格式错误。"""


class AclReadError(OSError):
    """读取文件访问控制项失败。"""


def _win_reason(code: int) -> str:
    detail = _KNOWN_WINERRORS.get(code) or ctypes.FormatError(code) or "未知错误"
    return f"[WinError {code}] {detail}"


# --------------------------------------------------------------------------- #
# ctypes 结构与 API 原型
# --------------------------------------------------------------------------- #
class ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", wintypes.BYTE),
        ("AceFlags", wintypes.BYTE),
        ("AceSize", wintypes.WORD),
    ]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", ACE_HEADER),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 按句柄读取安全信息（句柄由 CreateFileW 取得，因此共享冲突能被真实捕获）
advapi32.GetSecurityInfo.restype = wintypes.DWORD
advapi32.GetSecurityInfo.argtypes = [
    wintypes.HANDLE,                                     # handle
    wintypes.DWORD,                                      # ObjectType
    wintypes.DWORD,                                      # SecurityInfo
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),       # ppsidOwner
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),       # ppsidGroup
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),       # ppDacl
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),       # ppSacl
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),       # ppSecurityDescriptor
]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
advapi32.GetSecurityDescriptorDacl.argtypes = [
    ctypes.c_void_p,                     # pSecurityDescriptor
    ctypes.POINTER(wintypes.BOOL),       # lpbDaclPresent
    ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)),  # pDacl
    ctypes.POINTER(wintypes.BOOL),       # lpbDaclDefaulted
]

advapi32.GetAclInformation.restype = wintypes.BOOL
advapi32.GetAclInformation.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
]

advapi32.GetAce.restype = wintypes.BOOL
advapi32.GetAce.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]

advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.LPWSTR),
]

advapi32.LookupAccountSidW.restype = wintypes.BOOL
advapi32.LookupAccountSidW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]

kernel32.LocalFree.restype = wintypes.HLOCAL
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]


# --------------------------------------------------------------------------- #
# SID 解析与访问位判定
# --------------------------------------------------------------------------- #
def _sid_to_string(sid_ptr: ctypes.c_void_p) -> str:
    raw = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(raw)):
        return "UNKNOWN-SID"
    try:
        return raw.value or "UNKNOWN-SID"
    finally:
        kernel32.LocalFree(ctypes.cast(raw, wintypes.HLOCAL))


def _resolve_sid(sid_ptr: ctypes.c_void_p) -> tuple[str, str]:
    """返回 (展示名, SID 字符串)；展示名解析失败时退回 SID 字符串。"""
    sid_str = _sid_to_string(sid_ptr)
    name_len = wintypes.DWORD(256)
    domain_len = wintypes.DWORD(256)
    name = ctypes.create_unicode_buffer(name_len.value)
    domain = ctypes.create_unicode_buffer(domain_len.value)
    use = wintypes.DWORD(0)
    ok = advapi32.LookupAccountSidW(
        None, sid_ptr, name, ctypes.byref(name_len),
        domain, ctypes.byref(domain_len), ctypes.byref(use),
    )
    if not ok and ctypes.get_last_error() == 122:  # ERROR_INSUFFICIENT_BUFFER
        name = ctypes.create_unicode_buffer(name_len.value)
        domain = ctypes.create_unicode_buffer(domain_len.value)
        ok = advapi32.LookupAccountSidW(
            None, sid_ptr, name, ctypes.byref(name_len),
            domain, ctypes.byref(domain_len), ctypes.byref(use),
        )
    if ok:
        display = f"{domain.value}\\{name.value}" if domain.value else name.value
    else:
        display = sid_str
    return display, sid_str


def _classify_mask(mask: int) -> set[str]:
    """把 ACE 的访问掩码折算为 {'read','write'} 子集（仅数据面权限）。"""
    granted: set[str] = set()
    if mask & (GENERIC_READ | READ_BITS):
        granted.add("read")
    if mask & (GENERIC_WRITE | WRITE_BITS):
        granted.add("write")
    return granted


def _identity_matches(rule_identity: str, ace_sid: str, ace_display: str) -> bool:
    wanted = rule_identity.strip().lower()
    if wanted == ace_sid.lower():
        return True
    display = ace_display.lower()
    if wanted == display:
        return True
    if "\\" in display and display.split("\\", 1)[1] == wanted:
        return True
    return False


# --------------------------------------------------------------------------- #
# 读取文件 DACL
# --------------------------------------------------------------------------- #
def read_file_aces(path: str) -> list[dict]:
    """
    读取普通文件的允许型 ACE 列表，每项:
        {"sid": "S-1-...", "display": "DOMAIN\\name", "access": {"read","write"}}
    失败时抛出 AclReadError（含 WinError 与中文原因）。

    实现采用“先打开句柄、再按句柄取安全信息”的方式：
      - 优先以 GENERIC_READ 数据读权限打开，使文件被独占占用时
        CreateFileW 真实返回 WinError 32/33（共享冲突）；
      - 若账号本身没有数据读权限（WinError 5），回退为仅请求
        READ_CONTROL|SYNCHRONIZE —— 读取安全描述符只需要该权限，
        保证“能读元数据但不能读数据”的文件仍可完成核查；
      - 共享句柄位全开，不会反过来阻塞业务进程；
      - 拒绝型 ACE（deny）不授予访问，故不参与命中；对象/回调 ACE
        在普通文件上极罕见，遇到即按“无法可靠核查”处理，避免静默漏报。
    """
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if code == ERROR_ACCESS_DENIED:
            # 无数据读权限但可能仍有 READ_CONTROL：回退到元数据级打开
            handle = kernel32.CreateFileW(
                path,
                READ_CONTROL | SYNCHRONIZE,
                FILE_SHARE_ALL,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            code = ctypes.get_last_error()
        if handle == INVALID_HANDLE_VALUE:
            raise AclReadError(code, _win_reason(code))

    try:
        sd = ctypes.POINTER(ctypes.c_byte)()
        dacl = ctypes.POINTER(ctypes.c_byte)()
        rc = advapi32.GetSecurityInfo(
            handle, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(sd),
        )
        if rc != 0:
            raise AclReadError(rc, _win_reason(rc))
        try:
            present = wintypes.BOOL(False)
            defaulted = wintypes.BOOL(False)
            if not advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ):
                raise AclReadError(ctypes.get_last_error(),
                                   _win_reason(ctypes.get_last_error()))
            if not present or not dacl:
                # NULL DACL 表示“所有人完全控制”，无法归因到具体身份，
                # 本工具不生成风险记录，仅在技术说明中提示人工关注。
                return []

            info = ACL_SIZE_INFORMATION()
            if not advapi32.GetAclInformation(
                dacl, ctypes.byref(info), ctypes.sizeof(info),
                ACL_SIZE_INFORMATION_CLASS,
            ):
                raise AclReadError(ctypes.get_last_error(),
                                   _win_reason(ctypes.get_last_error()))

            aces: list[dict] = []
            for index in range(info.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)):
                    raise AclReadError(ctypes.get_last_error(),
                                       _win_reason(ctypes.get_last_error()))
                header = ctypes.cast(
                    ace_ptr, ctypes.POINTER(ACE_HEADER)
                )[0]
                if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
                    raise AclReadError(
                        0,
                        f"文件包含暂不支持解析的 ACE 类型 0x"
                        f"{header.AceType:02x}，为避免漏报转入未能核查清单",
                    )
                allowed = ctypes.cast(
                    ace_ptr, ctypes.POINTER(ACCESS_ALLOWED_ACE)
                )[0]
                sid_addr = ace_ptr.value + ACCESS_ALLOWED_ACE.SidStart.offset
                sid_ptr = ctypes.c_void_p(sid_addr)
                display, sid_str = _resolve_sid(sid_ptr)
                aces.append({
                    "sid": sid_str,
                    "display": display,
                    "access": _classify_mask(allowed.Mask),
                })
            return aces
        finally:
            kernel32.LocalFree(sd)
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# 规则加载（严格校验，非法即整体拒绝）
# --------------------------------------------------------------------------- #
def load_rules(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise RuleError(f"规则文件不存在: {path}") from exc
    except OSError as exc:
        raise RuleError(f"规则文件无法读取: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuleError(
            f"规则文件不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from exc

    if not isinstance(data, list):
        raise RuleError("规则文件顶层必须是 JSON 数组")

    rules: list[dict] = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise RuleError(f"第 {idx} 条规则必须是 JSON 对象")
        if set(item.keys()) != {"identity", "access"}:
            raise RuleError(
                f"第 {idx} 条规则字段必须且只能为 identity 与 access，"
                f"实际字段: {sorted(item.keys())}"
            )
        identity, access = item["identity"], item["access"]
        if not isinstance(identity, str) or not identity.strip():
            raise RuleError(f"第 {idx} 条规则的 identity 必须是非空字符串")
        if not isinstance(access, str) or access not in ("read", "write"):
            raise RuleError(
                f"第 {idx} 条规则的 access 只能是 read 或 write，"
                f"实际为 {access!r}"
            )
        rules.append({"identity": identity, "access": access})
    return rules


# --------------------------------------------------------------------------- #
# 递归遍历：跳过符号链接/联接点等 reparse point，不中断整体扫描
# --------------------------------------------------------------------------- #
def _is_reparse_point(entry: os.DirEntry) -> bool:
    """
    识别符号链接与目录联接点（junction）。

    注意：Python 的 DirEntry.is_symlink() 在 Windows 上只识别
    IO_REPARSE_TAG_SYMLINK，对 junction 返回 False，因此需要额外
    检查 FILE_ATTRIBUTE_REPARSE_POINT 属性位。
    """
    if entry.is_symlink():
        return True
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except OSError:
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _iter_regular_files(root_abs: str):
    """
    产出:
        ("file", 绝对路径)
        ("error", 绝对路径, 原因)      —— 子目录本身无法枚举时
    """
    stack = [root_abs]
    while stack:
        directory = stack.pop()
        try:
            scanner = os.scandir(directory)
        except OSError as exc:
            yield "error", os.path.abspath(directory), _os_error_reason(exc)
            continue
        try:
            with scanner:
                entries = list(scanner)
        except OSError as exc:
            yield "error", os.path.abspath(directory), _os_error_reason(exc)
            continue

        for entry in entries:
            path_abs = os.path.abspath(entry.path)
            try:
                # 符号链接与目录联接点（junction）均为 reparse point，一律跳过
                if _is_reparse_point(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield "file", path_abs
                # 其余类型（设备、套接字等）不属于普通文件，跳过
            except OSError as exc:
                yield "error", path_abs, _os_error_reason(exc)


def _os_error_reason(exc: OSError) -> str:
    code = getattr(exc, "winerror", None)
    if code:
        return _win_reason(code)
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 扫描与匹配
# --------------------------------------------------------------------------- #
def scan(root_abs: str, rules: list[dict]) -> tuple[list[dict], list[dict]]:
    risks: list[dict] = []
    unchecked: list[dict] = []

    for item in _iter_regular_files(root_abs):
        if item[0] == "error":
            _, path, reason = item
            unchecked.append({"file": path, "reason": reason})
            continue

        path = item[1]
        try:
            aces = read_file_aces(path)
        except AclReadError as exc:
            unchecked.append({"file": path, "reason": exc.strerror or str(exc)})
            continue
        except OSError as exc:
            unchecked.append({"file": path, "reason": _os_error_reason(exc)})
            continue
        except Exception as exc:  # 防御性兜底：单文件异常绝不中断整体扫描
            unchecked.append({
                "file": path,
                "reason": f"未预期异常 {type(exc).__name__}: {exc}",
            })
            continue

        # 逐条规则匹配；同一文件命中多条规则时分别记录，不做合并/去重
        for rule in rules:
            for ace in aces:
                if not _identity_matches(rule["identity"],
                                         ace["sid"], ace["display"]):
                    continue
                if rule["access"] in ace["access"]:
                    risks.append({
                        "file": path,
                        "identity": ace["display"],
                        "access": rule["access"],
                    })

    risks.sort(key=lambda r: (r["file"], r["identity"], r["access"]))
    unchecked.sort(key=lambda u: u["file"])
    return risks, unchecked


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="递归核查目录内普通文件的 Windows 访问控制项，对照基线规则输出风险清单。"
    )
    parser.add_argument("directory", help="待核查的目录路径")
    parser.add_argument("rules", help="JSON 基线规则文件路径")
    return parser


def _force_utf8_console() -> None:
    """
    显式固定标准流编码为 UTF-8。

    Windows 中文系统默认 OEM/GBK 代码页，重定向到文件时子进程还可能
    回退到系统 ANSI 编码，导致报告字节不确定。这里在输出前统一重配置，
    保证“报告以 JSON 输出到标准输出”在任何调用方式下均为 UTF-8；
    同时尝试把控制台代码页切到 UTF-8，让中文提示在交互终端也可读，
    切换失败（无控制台等场景）不影响已重配置的流编码。
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except OSError:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()

    if sys.platform != "win32":
        print("错误：本工具依赖 Windows 安全 API，仅支持在 Windows 上运行。",
              file=sys.stderr)
        return 2

    args = _build_parser().parse_args(argv)

    try:
        rules = load_rules(args.rules)
    except RuleError as exc:
        print(f"规则错误: {exc}", file=sys.stderr)
        return 2

    root_abs = os.path.abspath(args.directory)
    if not os.path.lexists(root_abs):
        print(f"目录不可用: 路径不存在: {root_abs}", file=sys.stderr)
        return 2
    if not os.path.isdir(root_abs):
        print(f"目录不可用: 路径不是目录: {root_abs}", file=sys.stderr)
        return 2
    try:
        with os.scandir(root_abs):
            pass
    except OSError as exc:
        print(f"目录不可用: 无法打开目录 {root_abs}: {_os_error_reason(exc)}",
              file=sys.stderr)
        return 2

    risks, unchecked = scan(root_abs, rules)
    json.dump(
        {"scan_root": root_abs, "risks": risks, "unchecked": unchecked},
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
