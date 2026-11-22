"""Pipeline orchestration, snapshots, reporting, and cross-module self-check."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import PipelineConfig
from .errors import (
    BusinessError,
    E_INCONSISTENT,
    E_INJECTION_INVALID,
    E_INJECTION_NOT_APPLIED,
    E_SNAPSHOT_ROLLBACK,
    SimulatedCrash,
)
from .injection import (
    STAGE_ORDER,
    InjectionSpec,
    InjectionTracker,
    normalize_injections,
)
from .samples import load_manifest, validate_manifest
from .serialization import canonical_bytes, checksum
from .snapshots import SnapshotRecord, SnapshotStore
from .stages import (
    STAGE_NAMES as STAGE_DISPLAY_NAMES,
    aggregate_page,
    detect_page,
    preprocess_page,
    recognize_page,
    unattributed_aggregate_page,
    unattributed_detect_page,
    unattributed_preprocess_page,
    unattributed_recognize_page,
)
from .templates import TemplateLibrary, get_template_library


PIPELINE_SCHEMA = "ocrlab-pipeline-v1"


def _version_rank(version: str) -> int:
    """Order known version labels such as glyph-v1 and glyph-v2."""
    index = len(version)
    while index > 0 and version[index - 1].isdigit():
        index -= 1
    suffix = version[index:]
    return int(suffix) if suffix else 0


def _stage_signature(
    stage: int,
    *,
    dataset_checksum: str,
    parent_signature: str | None,
    config: PipelineConfig,
    library: TemplateLibrary,
    injections: tuple[InjectionSpec, ...],
) -> str:
    config_dict = config.to_dict()
    if stage == 1:
        relevant = {
            key: config_dict[key]
            for key in ("binarize_threshold", "max_lines_per_page", "normalization_epsilon")
        }
    elif stage == 2:
        relevant = {
            key: config_dict[key]
            for key in ("line_min_ink", "line_padding")
        }
    elif stage == 3:
        relevant = {
            key: config_dict[key]
            for key in (
                "binarize_threshold",
                "recognition_chunk_lines",
                "max_candidate_comparisons",
                "max_feature_memory_bytes",
                "resource_policy",
                "candidates_per_char",
                "normalization_epsilon",
                "comparison_score_tolerance",
                "comparison_coordinate_tolerance",
            )
        }
    else:
        relevant = {
            key: config_dict[key]
            for key in (
                "aggregate_confidence_threshold",
                "fallback_confidence_threshold",
            )
        }
    payload = {
        "schema": PIPELINE_SCHEMA,
        "stage": stage,
        "stage_name": STAGE_DISPLAY_NAMES[stage],
        "dataset_checksum": dataset_checksum,
        "parent_signature": parent_signature,
        "config_version": config.version,
        "config_relevant": relevant,
        "injections": [spec.to_dict() for spec in injections if STAGE_ORDER[spec.stage] == stage],
    }
    if stage >= 3:
        payload["template_version"] = library.version
        payload["template_checksum"] = library.checksum
    return checksum(payload)


def _content(
    pages: dict[str, dict],
    injection_records: list[dict],
) -> dict[str, Any]:
    ordered_pages = {page_id: pages[page_id] for page_id in sorted(pages)}
    return {
        "schema_version": 1,
        "pages": ordered_pages,
        "page_ids": sorted(ordered_pages),
        "injection_records": injection_records,
    }


@dataclass
class RunResult:
    report: dict[str, Any]
    report_bytes: bytes
    injection_log: list[dict]
    injection_log_bytes: bytes
    selfcheck: dict[str, Any]
    snapshot_metadata: dict[str, Any]
    runtime: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        pages = []
        for page in self.report.get("pages", []):
            pages.append(
                {
                    "page_id": page["page_id"],
                    "status": page["status"],
                    "text": page["recognized_text"],
                    "truth_text": page["truth_text"],
                    "line_count": page["metrics"]["line_count"],
                    "mean_confidence": page["metrics"]["mean_confidence"],
                    "business_codes": page["business_codes"],
                }
            )
        return {
            "report_checksum": self.report.get("report_checksum"),
            "injection_count": len(self.injection_log),
            "pages": pages,
            "business_codes": self.report.get("business_codes", []),
            "selfcheck": self.selfcheck,
            "runtime": self.runtime,
            "snapshots": self.snapshot_metadata,
        }


class PipelineRunner:
    def __init__(
        self,
        data_dir: str | Path,
        state_dir: str | Path,
        *,
        config: PipelineConfig | None = None,
        library: TemplateLibrary | None = None,
        records: list[dict] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.config = config or PipelineConfig()
        self.config.validate()
        self.library = library or get_template_library("glyph-v1")
        self.records = (
            records if records is not None else load_manifest(self.data_dir)
        )
        self.records = sorted(self.records, key=lambda item: str(item["page_id"]))
        validate_manifest(self.records, self.config, self.library)
        self.store = SnapshotStore(self.state_dir)
        self.dataset_checksum = checksum(self.records)
        self.page_ids = tuple(record["page_id"] for record in self.records)

    def _validate_injection_targets(self, specs: tuple[InjectionSpec, ...]) -> None:
        by_id = {record["page_id"]: record for record in self.records}
        for spec in specs:
            if spec.page_id is None:
                continue
            page = by_id.get(spec.page_id)
            if page is None:
                raise BusinessError(
                    E_INJECTION_INVALID,
                    f"injection references unknown page_id: {spec.page_id}",
                    stage=spec.stage,
                    page_id=spec.page_id,
                    field="page_id",
                )
            if spec.line_no is not None and spec.line_no >= len(page["lines"]):
                raise BusinessError(
                    E_INJECTION_INVALID,
                    "injection line_no is outside the page",
                    stage=spec.stage,
                    page_id=spec.page_id,
                    line_no=spec.line_no,
                    field="line_no",
                )

    def _signatures(
        self,
        injections: tuple[InjectionSpec, ...],
        library: TemplateLibrary | None = None,
    ) -> list[str]:
        library = library or self.library
        signatures: list[str] = []
        parent: str | None = None
        for stage in range(1, 5):
            parent = _stage_signature(
                stage,
                dataset_checksum=self.dataset_checksum,
                parent_signature=parent,
                config=self.config,
                library=library,
                injections=injections,
            )
            signatures.append(parent)
        return signatures

    def _stage_one(self, tracker: InjectionTracker) -> tuple[dict[str, dict], list[dict]]:
        pages: dict[str, dict] = {}
        records: list[dict] = []
        for page in self.records:
            try:
                result = preprocess_page(page, self.config, self.library, tracker)
            except Exception as exc:
                result = unattributed_preprocess_page(page, self.config, exc)
            pages[page["page_id"]] = result
            records.extend(result["injection_records"])
        return pages, records

    def _stage_two(
        self,
        first: dict[str, dict],
        tracker: InjectionTracker,
    ) -> tuple[dict[str, dict], list[dict]]:
        pages: dict[str, dict] = {}
        records: list[dict] = []
        for page in self.records:
            try:
                result = detect_page(
                    page,
                    first[page["page_id"]],
                    self.config,
                    tracker,
                )
            except Exception as exc:
                result = unattributed_detect_page(page, exc)
            pages[page["page_id"]] = result
            records.extend(result["injection_records"])
        return pages, records

    def _stage_three(
        self,
        first: dict[str, dict],
        second: dict[str, dict],
        tracker: InjectionTracker,
        *,
        page_ids: set[str] | None = None,
        library: TemplateLibrary | None = None,
        reused_pages: dict[str, dict] | None = None,
    ) -> tuple[dict[str, dict], list[dict]]:
        library = library or self.library
        selected = page_ids if page_ids is not None else set(self.page_ids)
        pages = dict(reused_pages or {})
        records: list[dict] = []
        for page in self.records:
            page_id = page["page_id"]
            if page_id not in selected:
                continue
            try:
                result = recognize_page(
                    page,
                    first[page_id],
                    second[page_id],
                    self.config,
                    library,
                    tracker,
                )
            except Exception as exc:
                result = unattributed_recognize_page(
                    page,
                    second[page_id],
                    library,
                    exc,
                )
            pages[page_id] = result
            records.extend(result["injection_records"])
        ordered = {page_id: pages[page_id] for page_id in sorted(pages)}
        return ordered, records

    def _stage_four(
        self,
        third: dict[str, dict],
        tracker: InjectionTracker,
        *,
        page_ids: set[str] | None = None,
        reused_pages: dict[str, dict] | None = None,
    ) -> tuple[dict[str, dict], list[dict]]:
        selected = page_ids if page_ids is not None else set(self.page_ids)
        pages = dict(reused_pages or {})
        records: list[dict] = []
        for page in self.records:
            page_id = page["page_id"]
            if page_id not in selected:
                continue
            try:
                result = aggregate_page(
                    page,
                    third[page_id],
                    self.config,
                    tracker,
                )
            except Exception as exc:
                result = unattributed_aggregate_page(
                    page,
                    third[page_id],
                    exc,
                )
            pages[page_id] = result
            records.extend(result["injection_records"])
        ordered = {page_id: pages[page_id] for page_id in sorted(pages)}
        return ordered, records

    def _commit(
        self,
        stage: int,
        signature: str,
        pages: dict[str, dict],
        injection_records: list[dict],
        *,
        library: TemplateLibrary | None = None,
    ) -> SnapshotRecord:
        content = _content(pages, injection_records)
        active_library = library or self.library
        return self.store.commit_stage(
            stage,
            stage_name=STAGE_DISPLAY_NAMES[stage],
            signature=signature,
            content=content,
            expected_pages=set(self.page_ids),
            identity={
                "config_version": self.config.version,
                "config_checksum": self.config.config_checksum,
                "template_version": active_library.version,
                "template_checksum": active_library.checksum,
                "dataset_checksum": self.dataset_checksum,
            },
        )

    def run(
        self,
        injections: Iterable[dict[str, Any] | InjectionSpec] = (),
        *,
        reset: bool = False,
        crash_after_stage: int | None = None,
        upgrade_context: dict[str, Any] | None = None,
    ) -> RunResult:
        specs = normalize_injections(injections)
        self._validate_injection_targets(specs)
        if reset:
            self.store.invalidate_from(1)
        signatures = self._signatures(specs)
        tracker = InjectionTracker(specs)
        contents: dict[int, dict[str, dict]] = {}
        stage_records: dict[int, list[dict]] = {}
        stage_versions: dict[int, int] = {}
        reused_by_stage: dict[int, list[str]] = {}
        recomputed_by_stage: dict[int, list[str]] = {}
        resumed_from_stage = 5

        for stage in range(1, 5):
            compatible, stale = self.store.load_compatible(
                stage,
                signature=signatures[stage - 1],
                expected_pages=set(self.page_ids),
            )
            if compatible is not None:
                contents[stage] = compatible.content["pages"]
                stage_records[stage] = list(compatible.content.get("injection_records", []))
                stage_versions[stage] = compatible.version
                reused_by_stage[stage] = list(self.page_ids)
            else:
                if stale is not None:
                    self.store.invalidate_from(stage)
                if stage == 1:
                    pages, records = self._stage_one(tracker)
                elif stage == 2:
                    pages, records = self._stage_two(contents[1], tracker)
                elif stage == 3:
                    pages, records = self._stage_three(contents[1], contents[2], tracker)
                else:
                    pages, records = self._stage_four(contents[3], tracker)
                record = self._commit(stage, signatures[stage - 1], pages, records)
                contents[stage] = pages
                stage_records[stage] = records
                stage_versions[stage] = record.version
                recomputed_by_stage[stage] = list(self.page_ids)
                resumed_from_stage = min(resumed_from_stage, stage)
            if crash_after_stage == stage:
                self._validate_injections_applied(specs, stage_records, through_stage=stage)
                raise SimulatedCrash(stage)

        self._validate_injections_applied(specs, stage_records, through_stage=4)
        report, selfcheck = self._build_report(
            contents,
            stage_records,
            upgrade_context=upgrade_context,
        )
        report_bytes = canonical_bytes(report) + b"\n"
        injection_log = self._merge_injection_log(stage_records)
        injection_log_bytes = b"".join(
            canonical_bytes(record) + b"\n" for record in injection_log
        )
        runtime = {
            "resumed_from_stage": resumed_from_stage,
            "reused_pages_by_stage": {
                str(stage): pages for stage, pages in sorted(reused_by_stage.items())
            },
            "recomputed_pages_by_stage": {
                str(stage): pages for stage, pages in sorted(recomputed_by_stage.items())
            },
            "stage_versions": {
                str(stage): version for stage, version in sorted(stage_versions.items())
            },
            "injection_log_checksum": checksum(injection_log),
            "report_checksum": report["report_checksum"],
        }
        return RunResult(
            report=report,
            report_bytes=report_bytes,
            injection_log=injection_log,
            injection_log_bytes=injection_log_bytes,
            selfcheck=selfcheck,
            snapshot_metadata=self.store.metadata(),
            runtime=runtime,
        )

    def _merge_injection_log(self, stage_records: dict[int, list[dict]]) -> list[dict]:
        by_id: dict[str, dict] = {}
        for stage in sorted(stage_records):
            for record in stage_records[stage]:
                by_id[record["injection_id"]] = record
        return [by_id[key] for key in sorted(by_id)]

    def _validate_injections_applied(
        self,
        specs: tuple[InjectionSpec, ...],
        stage_records: dict[int, list[dict]],
        *,
        through_stage: int,
    ) -> None:
        applied = {
            record["injection_id"]
            for stage, records in stage_records.items()
            if stage <= through_stage
            for record in records
        }
        applicable = {
            spec.injection_id
            for spec in specs
            if STAGE_ORDER[spec.stage] <= through_stage
        }
        missing = sorted(applicable - applied)
        if missing:
            raise BusinessError(
                E_INJECTION_NOT_APPLIED,
                "one or more injections were not applied",
                stage=through_stage,
                details={"missing": missing},
            )

    def _build_report(
        self,
        contents: dict[int, dict[str, dict]],
        stage_records: dict[int, list[dict]],
        *,
        upgrade_context: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        injection_log = self._merge_injection_log(stage_records)
        stage_page_sets = {
            str(stage): sorted(contents[stage])
            for stage in range(1, 5)
        }
        pages = []
        for manifest in self.records:
            page_id = manifest["page_id"]
            stage_pages = {
                stage: contents[stage][page_id]
                for stage in range(1, 5)
            }
            issues = [
                issue
                for stage in range(1, 5)
                for issue in stage_pages[stage].get("issues", [])
            ]
            degradations = [
                degradation
                for stage in range(1, 5)
                for degradation in stage_pages[stage].get("degradations", [])
            ]
            final = stage_pages[4]
            reference_lines = [
                {
                    "line_index": line["line_index"],
                    "box": line["box"],
                    "text": line["text"],
                    "confidence": line["confidence"],
                }
                for line in final.get("reference_lines", [])
            ]
            lines = []
            for line in final.get("lines", []):
                lines.append(
                    {
                        "line_index": line["line_index"],
                        "box": line["box"],
                        "text": line["text"],
                        "confidence": line["confidence"],
                        "mean": line["mean"],
                        "std": line["std"],
                        "fallback": line.get("fallback"),
                        "fallback_trigger_code": line.get("fallback_trigger_code"),
                        "chars": line.get("chars", []),
                    }
                )
            codes = sorted(
                {
                    item["code"]
                    for item in issues
                    if item.get("attributed", True)
                }
                | {
                    item["trigger_code"]
                    for item in degradations
                    if item.get("trigger_code")
                }
            )
            pages.append(
                {
                    "page_id": page_id,
                    "status": final.get("status", "ok"),
                    "truth_text": "".join(line["text"] for line in manifest["lines"]),
                    "recognized_text": final.get("text", ""),
                    "reference_text": "".join(line["text"] for line in reference_lines),
                    "reference_lines": reference_lines,
                    "line_count": len(manifest["lines"]),
                    "metrics": final.get("metrics", {}),
                    "lines": lines,
                    "comparison": final.get("comparison", {}),
                    "issues": issues,
                    "degradations": degradations,
                    "attribution": [
                        {
                            "stage": issue.get("stage"),
                            "code": issue.get("code"),
                            "line_no": issue.get("line_no"),
                            "injected": issue.get("injected", False),
                            "attributed": issue.get("attributed", True),
                        }
                        for issue in issues
                    ],
                    "business_codes": codes,
                    "stage_present": {
                        str(stage): page_id in contents[stage]
                        for stage in range(1, 5)
                    },
                }
            )

        stage_summaries = []
        for stage in range(1, 5):
            page_values = list(contents[stage].values())
            stage_summaries.append(
                {
                    "stage": stage,
                    "stage_name": STAGE_DISPLAY_NAMES[stage],
                    "page_ids": sorted(contents[stage]),
                    "page_count": len(contents[stage]),
                    "status": "degraded"
                    if any(
                        page.get("issues") or page.get("degradations")
                        for page in page_values
                    )
                    else "ok",
                    "issue_count": sum(len(page.get("issues", [])) for page in page_values),
                    "degradation_count": sum(
                        len(page.get("degradations", [])) for page in page_values
                    ),
                }
            )
        all_codes = sorted(
            {
                code
                for page in pages
                for code in page["business_codes"]
            }
        )
        selfcheck = self._self_check(contents, pages, upgrade_context=upgrade_context)
        report_core = {
            "schema_version": 1,
            "pipeline_schema": PIPELINE_SCHEMA,
            "dataset_checksum": self.dataset_checksum,
            "expected_pages": list(self.page_ids),
            "stage_page_sets": stage_page_sets,
            "stages": stage_summaries,
            "config": {
                "version": self.config.version,
                "checksum": self.config.config_checksum,
                "effective": self.config.to_dict(),
            },
            "template_library": {
                "version": self.library.version,
                "checksum": self.library.checksum,
                "charset_versions": self.library.charset_versions,
            },
            "pages": pages,
            "injection_log": injection_log,
            "business_codes": all_codes,
            "selfcheck": selfcheck,
        }
        if upgrade_context is not None:
            report_core["library_upgrade"] = upgrade_context
        digest = checksum(report_core)
        report = dict(report_core)
        report["report_checksum"] = digest
        return report, selfcheck

    def _self_check(
        self,
        contents: dict[int, dict[str, dict]],
        pages: list[dict[str, Any]],
        *,
        upgrade_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        expected = set(self.page_ids)
        failures: list[dict[str, Any]] = []
        stage_sets = {stage: set(contents[stage]) for stage in range(1, 5)}
        for stage in range(1, 5):
            missing = sorted(expected - stage_sets[stage])
            extra = sorted(stage_sets[stage] - expected)
            if missing or extra:
                failures.append(
                    {
                        "code": E_INCONSISTENT,
                        "stage": STAGE_DISPLAY_NAMES[stage],
                        "page_id": (missing or extra)[0],
                        "message": "stage page set differs from the manifest",
                        "details": {"missing": missing, "extra": extra},
                    }
                )
        if any(stage_sets[stage] != stage_sets[1] for stage in range(2, 5)):
            baseline = stage_sets[1]
            for stage in range(2, 5):
                delta = sorted(stage_sets[stage] ^ baseline)
                if delta:
                    failures.append(
                        {
                            "code": E_INCONSISTENT,
                            "stage": STAGE_DISPLAY_NAMES[stage],
                            "page_id": delta[0],
                            "message": "stage page sets are not identical",
                        }
                    )
        for page in pages:
            for line in page["lines"]:
                if len(line["text"]) != len(line.get("chars", [])):
                    failures.append(
                        {
                            "code": E_INCONSISTENT,
                            "stage": "aggregate",
                            "page_id": page["page_id"],
                            "message": "recognized string length does not match character details",
                            "details": {"line_no": line["line_index"]},
                        }
                    )
                for char in line.get("chars", []):
                    if not char.get("candidates"):
                        failures.append(
                            {
                                "code": E_INCONSISTENT,
                                "stage": "recognize",
                                "page_id": page["page_id"],
                                "message": "character has no template candidate detail",
                                "details": {
                                    "line_no": line["line_index"],
                                    "char_position": char.get("index"),
                                },
                            }
                        )
            for issue in page["issues"]:
                if not issue.get("attributed", True):
                    failures.append(
                        {
                            "code": E_INCONSISTENT,
                            "stage": issue.get("stage", "unknown"),
                            "page_id": page["page_id"],
                            "message": "unattributed exception was recorded",
                            "details": {"business_code": issue.get("code")},
                        }
                    )
        if upgrade_context is not None:
            affected = set(upgrade_context.get("affected_pages", []))
            recomputed = set(upgrade_context.get("recomputed_pages", []))
            reused = set(upgrade_context.get("reused_pages", []))
            if recomputed & reused:
                failures.append(
                    {
                        "code": E_INCONSISTENT,
                        "stage": "recognize",
                        "page_id": sorted(recomputed & reused)[0],
                        "message": "recomputed and reused page sets overlap",
                    }
                )
            if affected - (recomputed | reused):
                failures.append(
                    {
                        "code": E_INCONSISTENT,
                        "stage": "recognize",
                        "page_id": sorted(affected - (recomputed | reused))[0]
                        if affected - (recomputed | reused)
                        else None,
                        "message": "recomputed/reused union does not cover affected pages",
                        "details": {
                            "affected": sorted(affected),
                            "union": sorted(recomputed | reused),
                        },
                    }
                )
            if recomputed - affected:
                failures.append(
                    {
                        "code": E_INCONSISTENT,
                        "stage": "recognize",
                        "page_id": sorted(recomputed - affected)[0],
                        "message": "recomputed pages are outside the affected page set",
                    }
                )
        if failures:
            first = failures[0]
            raise BusinessError(
                E_INCONSISTENT,
                "cross-module consistency self-check failed",
                stage=first.get("stage"),
                page_id=first.get("page_id"),
                field="selfcheck",
                details={"failures": failures},
            )
        return {
            "ok": True,
            "checks": [
                "stage_page_sets_identical",
                "recognized_length_matches_character_details",
                "candidate_details_present",
                "all_exceptions_attributed",
                "recompute_reuse_disjoint_and_cover_affected",
            ],
            "page_count": len(expected),
            "stage_page_counts": {
                str(stage): len(contents[stage]) for stage in range(1, 5)
            },
            "affected_pages": upgrade_context.get("affected_pages", [])
            if upgrade_context
            else [],
            "recomputed_pages": upgrade_context.get("recomputed_pages", [])
            if upgrade_context
            else [],
            "reused_pages": upgrade_context.get("reused_pages", [])
            if upgrade_context
            else [],
        }

    def upgrade_template_library(
        self,
        new_library: TemplateLibrary,
        *,
        injections: Iterable[dict[str, Any] | InjectionSpec] = (),
    ) -> RunResult:
        """Upgrade templates and recompute only pages containing changed glyphs."""
        old_rank = _version_rank(self.library.version)
        new_rank = _version_rank(new_library.version)
        if new_rank < old_rank:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "template library version rollback is not permitted",
                stage="recognize",
                field="template_version",
                details={
                    "active_version": self.library.version,
                    "requested_version": new_library.version,
                },
            )
        specs = normalize_injections(injections)
        self._validate_injection_targets(specs)
        old_signatures = self._signatures(specs)
        new_signatures = self._signatures(specs, library=new_library)
        expected = set(self.page_ids)
        first_record = self.store.load_compatible(
            1, signature=old_signatures[0], expected_pages=expected
        )[0]
        second_record = self.store.load_compatible(
            2, signature=old_signatures[1], expected_pages=expected
        )[0]
        third_record = self.store.load(3, expected_pages=expected)
        fourth_record = self.store.load(4, expected_pages=expected)
        if any(
            item is None
            for item in (first_record, second_record, third_record, fourth_record)
        ):
            raise BusinessError(
                E_INCONSISTENT,
                "template upgrade requires complete committed stage snapshots",
                stage="recognize",
                field="snapshots",
            )
        assert first_record and second_record and third_record and fourth_record
        changed = self.library.changed_characters(new_library)
        changed_set = set(changed)
        affected = sorted(
            page["page_id"]
            for page in self.records
            if any(set(line["text"]) & changed_set for line in page["lines"])
        )
        affected_set = set(affected)
        unaffected = expected - affected_set
        third_old_pages = third_record.content["pages"]
        fourth_old_pages = fourth_record.content["pages"]
        tracker = InjectionTracker(specs)
        third_recomputed, third_records = self._stage_three(
            first_record.content["pages"],
            second_record.content["pages"],
            tracker,
            page_ids=affected_set,
            library=new_library,
            reused_pages={
                page_id: third_old_pages[page_id] for page_id in sorted(unaffected)
            },
        )
        third_commit = self.store.commit_stage(
            3,
            stage_name=STAGE_DISPLAY_NAMES[3],
            signature=new_signatures[2],
            content=_content(third_recomputed, third_records),
            expected_pages=expected,
            identity={
                "config_version": self.config.version,
                "config_checksum": self.config.config_checksum,
                "template_version": new_library.version,
                "template_checksum": new_library.checksum,
                "dataset_checksum": self.dataset_checksum,
            },
        )
        fourth_recomputed, fourth_records = self._stage_four(
            third_recomputed,
            tracker,
            page_ids=affected_set,
            reused_pages={
                page_id: fourth_old_pages[page_id] for page_id in sorted(unaffected)
            },
        )
        fourth_commit = self.store.commit_stage(
            4,
            stage_name=STAGE_DISPLAY_NAMES[4],
            signature=new_signatures[3],
            content=_content(fourth_recomputed, fourth_records),
            expected_pages=expected,
            identity={
                "config_version": self.config.version,
                "config_checksum": self.config.config_checksum,
                "template_version": new_library.version,
                "template_checksum": new_library.checksum,
                "dataset_checksum": self.dataset_checksum,
            },
        )
        self._validate_injections_applied(specs, {3: third_records, 4: fourth_records}, through_stage=4)
        stage_records = {
            1: list(first_record.content.get("injection_records", [])),
            2: list(second_record.content.get("injection_records", [])),
            3: third_records,
            4: fourth_records,
        }
        contents = {
            1: first_record.content["pages"],
            2: second_record.content["pages"],
            3: third_recomputed,
            4: fourth_recomputed,
        }
        upgrade_context = {
            "changed_characters": list(changed),
            "affected_pages": affected,
            "recomputed_pages": affected,
            "reused_pages": sorted(unaffected),
            "old_template_version": self.library.version,
            "old_template_checksum": self.library.checksum,
            "new_template_version": new_library.version,
            "new_template_checksum": new_library.checksum,
        }
        old_library = self.library
        self.library = new_library
        try:
            report, selfcheck = self._build_report(
                contents,
                stage_records,
                upgrade_context=upgrade_context,
            )
        finally:
            self.library = old_library
        report_bytes = canonical_bytes(report) + b"\n"
        injection_log = self._merge_injection_log(stage_records)
        runtime = {
            "resumed_from_stage": 3,
            "reused_pages_by_stage": {"3": sorted(unaffected), "4": sorted(unaffected)},
            "recomputed_pages_by_stage": {"3": affected, "4": affected},
            "stage_versions": {
                "1": first_record.version,
                "2": second_record.version,
                "3": third_commit.version,
                "4": fourth_commit.version,
            },
            "injection_log_checksum": checksum(injection_log),
            "report_checksum": report["report_checksum"],
            "upgrade": upgrade_context,
        }
        return RunResult(
            report=report,
            report_bytes=report_bytes,
            injection_log=injection_log,
            injection_log_bytes=b"".join(
                canonical_bytes(record) + b"\n" for record in injection_log
            ),
            selfcheck=selfcheck,
            snapshot_metadata=self.store.metadata(),
            runtime=runtime,
        )

