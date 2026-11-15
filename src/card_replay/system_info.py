from __future__ import annotations

import os
import sys


def detected_memory_bytes() -> int | None:
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except (AttributeError, OSError):
            return None
        return None

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        return page_size * available_pages
    except (AttributeError, OSError, ValueError):
        return None


def system_summary() -> dict[str, int | None]:
    memory_bytes = detected_memory_bytes()
    return {
        "cpu_count": os.cpu_count(),
        "available_memory_bytes": memory_bytes,
        "available_memory_mib": memory_bytes // (1024 * 1024)
        if memory_bytes is not None
        else None,
    }
