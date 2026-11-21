"""Process-level resource observation via psutil (observability only).

Admission decisions stay deterministic and config-driven; psutil metrics are
recorded alongside each scheduling decision so reports show the real resident
footprint at arbitration time without making math depend on host state.
"""

from __future__ import annotations

import psutil


def current_process_metrics() -> dict:
    proc = psutil.Process()
    memory = proc.memory_info()
    vm = psutil.virtual_memory()
    return {
        "pid": proc.pid,
        "rss_bytes": int(memory.rss),
        "vms_bytes": int(memory.vms),
        "system_total_bytes": int(vm.total),
        "system_available_bytes": int(vm.available),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
    }

