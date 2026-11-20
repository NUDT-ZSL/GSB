"""High-level diagnostic workbench facade.

The workbench owns the dataset directory, content cache, template snapshot
store and engine.  It exposes whole-page and chunked recognition, incremental
change scenarios, self checks and a structured demo that the Tk UI and CLI
both consume.  Recent business errors are retained for the workbench display.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cache import ContentCache
from .config import PipelineConfig
from .engine import OcrEngine, PageRunResult
from .errors import ErrorCode, OcrBenchError, make_error
from .incremental import ChangeSet, IncrementalPlanner, InvalidationPlan
from .samplegen import (
    LineTruth,
    PageSample,
    generate_page,
    load_manifest,
    load_page,
    write_dataset,
)
from .snapshot import TemplateSnapshotStore
from .deterministic import content_hash


@dataclass
class ErrorEvent:
    code: str
    message: str
    page_id: Optional[str]
    stage: Optional[str]
    details: Dict[str, Any]


class OcrWorkbench:
    def __init__(
        self,
        root: str,
        config: Optional[PipelineConfig] = None,
    ) -> None:
        self.root = root
        self.data_dir = os.path.join(root, "data")
        self.cache_dir = os.path.join(root, "cache")
        self.snapshot_dir = os.path.join(root, "snapshot")
        os.makedirs(root, exist_ok=True)
        self.config = config or PipelineConfig()
        self.cache = ContentCache(
            self.cache_dir, self.config.resource_limits.max_cache_entries
        )
        self.snapshots = TemplateSnapshotStore(self.snapshot_dir)
        self.engine = OcrEngine(self.cache, self.snapshots, self.config)
        self.planner = IncrementalPlanner(self.engine.plan)
        self.recent_errors: List[ErrorEvent] = []
        self._pages: Dict[str, PageSample] = {}

    # ------------------------------------------------------------- errors
    def record_error(self, error: OcrBenchError) -> None:
        self.recent_errors.append(
            ErrorEvent(
                code=error.code,
                message=error.message,
                page_id=error.page_id,
                stage=error.stage,
                details=dict(error.details),
            )
        )
        self.recent_errors = self.recent_errors[-12:]

    def safe_call(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except OcrBenchError as error:
            self.record_error(error)
            raise

    # ------------------------------------------------------------ dataset
    def create_dataset(
        self, seeds: Sequence[Tuple[str, int]], charset_version: int = 1
    ) -> List[PageSample]:
        pages = write_dataset(
            self.data_dir, seeds, charset_version=charset_version
        )
        self._pages = {page.page_id: page for page in pages}
        # Persist the matching initial template snapshot.
        self.engine.ensure_library(charset_version)
        return pages

    def load_dataset(self) -> List[PageSample]:
        records = self.safe_call(load_manifest, self.data_dir)
        pages = [load_page(self.data_dir, record) for record in records]
        self._pages = {page.page_id: page for page in pages}
        return pages

    def append_page(self, page_id: str, image_seed: int) -> PageSample:
        """Append a new page to the manifest and persist its pixels/truth."""

        records = load_manifest(self.data_dir)
        existing = {record["page_id"] for record in records}
        if page_id in existing:
            raise make_error(
                ErrorCode.PARSE_FAILED,
                "page_id already exists in manifest",
                page_id=page_id,
                stage="manifest",
            )
        page = generate_page(
            page_id,
            image_seed,
            charset_version=self.config.charset_version,
        )
        import json
        import numpy as np

        pages_dir = os.path.join(self.data_dir, "pages")
        truth_dir = os.path.join(self.data_dir, "truth")
        os.makedirs(pages_dir, exist_ok=True)
        os.makedirs(truth_dir, exist_ok=True)
        np.savez_compressed(os.path.join(pages_dir, f"{page_id}.npz"), image=page.image)
        with open(os.path.join(truth_dir, f"{page_id}.json"), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "page_id": page.page_id,
                    "lines": [line.to_dict() for line in page.lines],
                },
                handle,
                sort_keys=True,
            )
        with open(os.path.join(self.data_dir, "manifest.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(page.manifest_record(), sort_keys=True) + "\n")
        self._pages[page_id] = page
        return page

    def correct_truth(self, page_id: str, line_index: int, new_text: str) -> None:
        """Override one ground-truth line in memory and on disk."""

        import json

        page = self._require_page(page_id)
        if not 0 <= line_index < len(page.lines):
            raise make_error(
                ErrorCode.PARSE_FAILED,
                "line index out of range for truth correction",
                page_id=page_id,
                stage="truth",
                line_index=line_index,
            )
        lines = list(page.lines)
        target = lines[line_index]
        lines[line_index] = LineTruth(
            line_index=target.line_index,
            text=new_text,
            bbox=target.bbox,
        )
        page.lines = lines
        with open(
            os.path.join(self.data_dir, "truth", f"{page_id}.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                {
                    "page_id": page.page_id,
                    "lines": [line.to_dict() for line in page.lines],
                },
                handle,
                sort_keys=True,
            )

    def _require_page(self, page_id: str) -> PageSample:
        if not self._pages:
            self.load_dataset()
        if page_id not in self._pages:
            raise make_error(
                ErrorCode.PARSE_FAILED,
                "unknown page_id",
                page_id=page_id,
                stage="dataset",
            )
        return self._pages[page_id]

    def page_ids(self) -> List[str]:
        if not self._pages:
            self.load_dataset()
        return sorted(self._pages)

    # -------------------------------------------------------------- runs
    def run_all(self) -> Dict[str, PageRunResult]:
        results: Dict[str, PageRunResult] = {}
        for page_id in self.page_ids():
            page = self._require_page(page_id)
            results[page_id] = self.safe_call(self.engine.run_page, page)
        return results

    def run_page(self, page_id: str) -> PageRunResult:
        page = self._require_page(page_id)
        return self.safe_call(self.engine.run_page, page)

    def run_chunked(self, page_id: str, chunk_size: int) -> Dict[str, Any]:
        page = self._require_page(page_id)
        return self.safe_call(self.engine.run_page_chunked, page, chunk_size)

    # ------------------------------------------------------- incremental
    def plan_incremental(self, change_set: ChangeSet) -> InvalidationPlan:
        return self.planner.plan_changes(self.page_ids(), change_set)

    def node_status(self, result: PageRunResult) -> Dict[str, str]:
        return {
            execution.node: ("reused" if execution.reused else "recomputed")
            for execution in result.executions
        }

    # ------------------------------------------------------------- metrics
    def metrics(self) -> Dict[str, Any]:
        aggregate_results: List[Dict[str, Any]] = []
        for page_id in self.page_ids():
            result = self.run_page(page_id)
            aggregate_results.append(result.payloads["aggregate"])
        total_correct = sum(item["correct_chars"] for item in aggregate_results)
        total_chars = sum(item["total_truth_chars"] for item in aggregate_results)
        total_exact = sum(item["exact_lines"] for item in aggregate_results)
        total_lines = sum(item["total_lines"] for item in aggregate_results)
        scale = self.config.confidence_scale
        return {
            "char_accuracy": (total_correct / total_chars) if total_chars else 0.0,
            "line_accuracy": (total_exact / total_lines) if total_lines else 0.0,
            "char_accuracy_q": round(
                (total_correct / total_chars) * scale if total_chars else 0.0
            ),
            "line_accuracy_q": round(
                (total_exact / total_lines) * scale if total_lines else 0.0
            ),
            "page_count": len(aggregate_results),
            "total_lines": total_lines,
        }

    def template_version(self) -> Optional[int]:
        return self.snapshots.current_version()

    def inspect_node(self, page_id: str, node: str) -> Dict[str, Any]:
        """Return cache key components and content hash for a node/page."""

        result = self.run_page(page_id)
        for execution in result.executions:
            if execution.node == node:
                return {
                    "node": node,
                    "page_id": page_id,
                    "cache_key": execution.key,
                    "content_hash": execution.content_hash,
                    "reused": execution.reused,
                    "key_details": execution.key_details,
                }
        raise make_error(
            ErrorCode.INCONSISTENT,
            "node not found in page execution",
            page_id=page_id,
            stage=node,
        )
