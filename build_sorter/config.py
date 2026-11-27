"""Validated command-line configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import SorterError


@dataclass(slots=True)
class SortConfig:
    db_path: Path
    work_dir: Path
    mem_limit_bytes: int
    max_fan_in: int
    source_table: str = "build_task"
    result_table: str = "build_task_sorted"
    output_db_path: Path | None = None
    report_path: Path | None = None
    checkpoint_records: int = 1000
    resume: bool = False
    verify_segments: bool = True

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.work_dir = Path(self.work_dir).expanduser().resolve()
        self.output_db_path = (
            Path(self.output_db_path).expanduser().resolve()
            if self.output_db_path is not None
            else self.db_path
        )
        self.report_path = (
            Path(self.report_path).expanduser().resolve()
            if self.report_path is not None
            else self.work_dir / "sort_report.json"
        )
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.mem_limit_bytes, int) or self.mem_limit_bytes <= 0:
            raise SorterError(
                "INVALID_MEMORY_LIMIT",
                "--mem-limit-bytes must be a positive integer",
            )
        if not isinstance(self.max_fan_in, int) or self.max_fan_in < 2:
            raise SorterError(
                "INVALID_FAN_IN",
                "--max-fan-in must be an integer greater than or equal to 2",
            )
        if not isinstance(self.checkpoint_records, int) or self.checkpoint_records <= 0:
            raise SorterError(
                "INVALID_CHECKPOINT_INTERVAL",
                "--checkpoint-records must be a positive integer",
            )
        for value, name in (
            (self.source_table, "--source-table"),
            (self.result_table, "--result-table"),
        ):
            if not value or "\x00" in value:
                raise SorterError("INVALID_IDENTIFIER", f"{name} is not a valid identifier")
        if self.report_path in {self.db_path, self.output_db_path}:
            raise SorterError(
                "INVALID_REPORT_PATH",
                "--report must not point at a SQLite database",
            )
        if (
            self.report_path.parent == self.work_dir
            and self.report_path.name == "sort_manifest.json"
        ):
            raise SorterError(
                "INVALID_REPORT_PATH",
                "--report must not overwrite the sort manifest",
            )
        if (
            self.source_table.casefold() == self.result_table.casefold()
            and self.output_db_path == self.db_path
        ):
            raise SorterError(
                "RESULT_TABLE_IS_SOURCE",
                "the result table must not overwrite the source build_task table",
            )

    @property
    def effective_report_path(self) -> Path:
        assert self.report_path is not None
        return self.report_path
