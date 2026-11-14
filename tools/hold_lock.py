"""用 Win32 共享锁占用指定文件，模拟“文件被写锁占用”的现场（需求 5）。

原理：以 0 共享模式打开文件，其他进程再打开时会得到共享违例
（WinError 32），reader 模块据此判定为 locked。
仅用于本地演示，默认持有 60 秒，可用 --seconds 调整。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_NONE = 0
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def hold_lock(file_path: Path, seconds: int) -> int:
    if sys.platform != "win32":
        print("锁占用模拟仅支持 Windows", file=sys.stderr)
        return 1

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = kernel32.CreateFileW
    CreateFileW.restype = wintypes.HANDLE
    CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]

    handle = CreateFileW(
        str(file_path.resolve()),
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_NONE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        print(f"无法打开文件加锁: {file_path} (WinError {err})", file=sys.stderr)
        return 1

    print(f"已锁定 {file_path.name}，持有 {seconds} 秒 ...", flush=True)
    try:
        time.sleep(seconds)
    finally:
        kernel32.CloseHandle(handle)
        print(f"已释放 {file_path.name}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows 文件写锁模拟工具")
    parser.add_argument("file")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    return hold_lock(Path(args.file), args.seconds)


if __name__ == "__main__":
    raise SystemExit(main())
