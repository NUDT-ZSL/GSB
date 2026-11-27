"""Convenience module and entry point for the build-task external sorter."""

from build_sorter import (
    SortConfig,
    SorterError,
    cleanup_after_failure,
    run_all,
    run_merge_phase,
    run_spill_phase,
    sort_database,
)
from build_sorter.cli import main

__all__ = [
    "SortConfig",
    "SorterError",
    "cleanup_after_failure",
    "run_all",
    "run_merge_phase",
    "run_spill_phase",
    "sort_database",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
