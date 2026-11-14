#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
占锁辅助程序：以“拒绝共享”方式独占打开目标文件并保持指定秒数。

用于制造需求 8 中的共享冲突（WinError 32）场景。
用法: python lock_holder.py <文件路径> <保持秒数>
"""

import ctypes
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: lock_holder.py <文件路径> <保持秒数>", file=sys.stderr)
        return 2
    path, seconds = sys.argv[1], int(sys.argv[2])

    # dwShareMode=0：不允许任何后续共享打开，触发共享冲突
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        print(f"占锁失败: [WinError {code}] {ctypes.FormatError(code)}",
              file=sys.stderr)
        return 1

    print(f"LOCKED {path} for {seconds}s", flush=True)
    try:
        time.sleep(seconds)
    finally:
        kernel32.CloseHandle(handle)
        print(f"RELEASED {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
