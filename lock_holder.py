#!/usr/bin/env python3
"""
lock_holder.py — 以独占方式打开目标文件并保持指定秒数。

Windows 上 CreateFile 传 dwShareMode=0 后，其他进程（含扫描器）打开该文件会
触发 PermissionError(WinError 32/33)，用于验收点 5/9 的“无法读取”场景。
进程退出后句柄自动释放，不留残余锁定。

用法：
    python -I lock_holder.py --file <待独占文件> --seconds 15
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import time
from ctypes import get_last_error

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_SHARE_NONE = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def hold_exclusive(path, seconds):
    if not sys.platform.startswith("win"):
        raise SystemExit("lock_holder.py is only supported on Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(
        os.path.abspath(path), GENERIC_READ, FILE_SHARE_NONE,
        None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise SystemExit(f"CreateFileW failed, WinError {get_last_error()}")
    try:
        print(f"exclusive lock held on {path} for {seconds}s", flush=True)
        time.sleep(seconds)
    finally:
        kernel32.CloseHandle(handle)
        print("lock released", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="独占打开文件以模拟不可读场景")
    ap.add_argument("--file", required=True)
    ap.add_argument("--seconds", type=float, default=15.0)
    ns = ap.parse_args()
    hold_exclusive(ns.file, ns.seconds)
