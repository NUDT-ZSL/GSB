"""规范化处理管线：映射、校验、去重合并、冲突裁决与续跑状态。

合并 / 裁决规则（需求 3、需求 7 共用同一套次序）：
1. 业务时间最新者胜；
2. 业务时间相同，来源优先级数字最小者胜（配置约定 1 为最高优先级）；
3. 仍相同则按文件名、行号确定唯一次序，保证结果可重复。
不同来源的同主键同字段取值不同时，额外记录 field_conflict 事件。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, SourceConfig
from .errors import PipelineError
from .reader import (
    FileReadResult,
    STATUS_LOCKED,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_SOURCE_UNMATCHED,
    STATUS_TRUNCATED,
    is_unfinished,
    read_jsonl_file,
)
from .tzutil import format_canonical, parse_datetime, resolve_timezone

STATE_FILE_NAME = ".handover_state.json"
RECORDS_FILE_NAME = "canonical_records.jsonl"
REPORT_JSON_NAME = "handover_report.json"
REPORT_MD_NAME = "handover_report.md"

EVENT_MERGE = "duplicate_merge"
EVENT_CONFLICT = "field_conflict"
EVENT_REJECTION = "missing_required"


@dataclass(frozen=True)
class Candidate:
    pk: str
    record: dict
    business_time: datetime
    business_time_str: str
    source_name: str
    priority: int
    file_name: str
    line_no: int

    def rank(self) -> tuple:
        # 业务时间降序 -> 优先级升序 -> 文件名升序 -> 行号升序
        return (
            -self.business_time.timestamp(),
            self.priority,
            self.file_name,
            self.line_no,
        )


@dataclass
class Rejection:
    pk_display: str
    file_name: str
    line_no: int
    missing_fields: list[str]


@dataclass
class IngestResult:
    """单文件摄入结果。"""
    file_name: str
    source_name: str | None
    status: str
    detail: str
    size_bytes: int
    candidates: list[Candidate] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    bad_line_no: int | None = None
    bad_line_preview: str = ""
    reused: bool = False  # 本次运行是否直接复用了历史状态


def _fingerprint(path: Path) -> str | None:
    """计算文件 SHA-256；文件被占用无法读取时返回 None（锁检测由读取器负责）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _map_one(
    raw: dict,
    source: SourceConfig,
    config: Config,
    default_tz,
    global_default_tz,
    *,
    file_name: str,
    line_no: int,
) -> tuple[Candidate | None, Rejection | None]:
    """把一条来源记录映射为候选；缺必填字段时返回 Rejection。"""
    mapped: dict[str, object] = {}
    for src_field, target in source.mappings.items():
        if src_field in raw and not _is_blank(raw[src_field]):
            mapped[target] = raw[src_field]
    # 未在 mappings 中声明的来源字段一律丢弃（需求 1：按配置丢弃或保留）

    # 日期字段统一格式（需求 2）
    for date_field in config.date_fields:
        if date_field in mapped:
            where = f"{file_name} 第 {line_no} 行字段 {date_field}"
            try:
                # 解析时用来源时区解释裸日期；输出统一换算到全局默认时区
                dt, _ = parse_datetime(
                    mapped[date_field],
                    source.input_date_formats,
                    default_tz,
                    where=where,
                )
            except Exception as exc:
                # 数据内容无法解析按配置错误处理：给出明确位置，中止本次运行
                raise PipelineError(str(exc)) from None
            dt = dt.astimezone(global_default_tz)
            mapped[date_field] = format_canonical(dt)

    pk_value = mapped.get(config.primary_key)
    if _is_blank(pk_value):
        pk_display = f"<缺少主键:{file_name}:L{line_no}>"
        pk = pk_display
    else:
        pk = str(pk_value)
        pk_display = pk

    missing = [f for f in config.required_fields if _is_blank(mapped.get(f))]
    if missing:
        return None, Rejection(
            pk_display=pk_display,
            file_name=file_name,
            line_no=line_no,
            missing_fields=missing,
        )

    # 业务时间：配置未声明日期字段时退化为固定最小值，仍可完成确定性合并
    if config.date_fields:
        # 业务时间取第一个声明的日期字段
        bt_field = config.date_fields[0]
        bt_str = mapped[bt_field]
        bt_dt = datetime.fromisoformat(bt_str)
    else:
        bt_field = ""
        bt_str = ""
        bt_dt = datetime.min.replace(tzinfo=default_tz)

    ordered = {f: mapped[f] for f in config.canonical_fields if f in mapped}
    candidate = Candidate(
        pk=pk,
        record=ordered,
        business_time=bt_dt,
        business_time_str=bt_str,
        source_name=source.name,
        priority=source.priority,
        file_name=file_name,
        line_no=line_no,
    )
    return candidate, None


def _candidate_to_state(c: Candidate) -> dict:
    return {
        "pk": c.pk,
        "record": c.record,
        "business_time": c.business_time.timestamp(),
        "business_time_str": c.business_time_str,
        "source_name": c.source_name,
        "priority": c.priority,
        "file_name": c.file_name,
        "line_no": c.line_no,
    }


def _candidate_from_state(d: dict) -> Candidate:
    # timestamp() 是绝对时刻，按 UTC 还原为带时区 datetime
    return Candidate(
        pk=d["pk"],
        record=dict(d["record"]),
        business_time=datetime.fromtimestamp(d["business_time"], tz=timezone.utc),
        business_time_str=d["business_time_str"],
        source_name=d["source_name"],
        priority=d["priority"],
        file_name=d["file_name"],
        line_no=d["line_no"],
    )


def _rejection_to_state(r: Rejection) -> dict:
    return {
        "pk_display": r.pk_display,
        "file_name": r.file_name,
        "line_no": r.line_no,
        "missing_fields": list(r.missing_fields),
    }


def _rejection_from_state(d: dict) -> Rejection:
    return Rejection(
        pk_display=d["pk_display"],
        file_name=d["file_name"],
        line_no=d["line_no"],
        missing_fields=list(d["missing_fields"]),
    )


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + 原子替换，避免写出半截产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class Pipeline:
    def __init__(self, config: Config, input_dir: Path, output_dir: Path,
                 *, run_at: str | None = None):
        self.config = config
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.run_at = run_at  # 仅用于报告时间戳，便于确定性演示
        self.state_path = output_dir / STATE_FILE_NAME
        self.records_path = output_dir / RECORDS_FILE_NAME
        # 需求 2：所有来源的日期统一换算到全局默认时区后再格式化
        self.default_tz = resolve_timezone(
            config.default_timezone_name, where="$.default_timezone"
        )
        self._tz_cache: dict[str, object] = {}

    def _tz_for(self, source: SourceConfig | None):
        """来源默认时区：仅用于解释“不带时区标记”的日期值。"""
        name = source.default_tz_name if source else self.config.default_timezone_name
        if name not in self._tz_cache:
            self._tz_cache[name] = resolve_timezone(
                name, where=f"sources.{source.name if source else ''}.default_timezone"
            )
        return self._tz_cache[name]

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {"version": 1, "files": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PipelineError(
                f"历史状态文件 {self.state_path} 已损坏：{exc.msg}；"
                "请人工确认后删除该文件再重跑"
            ) from None
        if not isinstance(data, dict) or "files" not in data:
            raise PipelineError(
                f"历史状态文件 {self.state_path} 结构不正确，无法续跑"
            )
        return data

    def _save_state(self, state: dict) -> None:
        atomic_write_text(
            self.state_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    # ------------------------------------------------------------- per-file
    def _ingest_file(
        self, path: Path, source: SourceConfig | None, old_entry: dict | None
    ) -> IngestResult:
        read_result: FileReadResult = read_jsonl_file(path)

        if read_result.status != STATUS_OK:
            # 续跑：文件仍不可读且历史上成功过 -> 沿用历史数据，保证已成功记录不丢
            if old_entry and old_entry.get("status") == STATUS_OK:
                return self._reuse_entry(old_entry, read_result)
            return IngestResult(
                file_name=path.name,
                source_name=source.name if source else None,
                status=read_result.status,
                detail=read_result.detail,
                size_bytes=read_result.size_bytes,
                bad_line_no=read_result.bad_line_no,
                bad_line_preview=read_result.bad_line_preview,
            )

        if source is None:
            return IngestResult(
                file_name=path.name,
                source_name=None,
                status=STATUS_SOURCE_UNMATCHED,
                detail="没有任何来源配置的 file_match 与该文件名匹配",
                size_bytes=read_result.size_bytes,
            )

        tz = self._tz_for(source)
        candidates: list[Candidate] = []
        rejections: list[Rejection] = []
        for raw_rec in read_result.records:
            candidate, rejection = _map_one(
                raw_rec.data, source, self.config, tz, self.default_tz,
                file_name=path.name, line_no=raw_rec.line_no,
            )
            if candidate is not None:
                candidates.append(candidate)
            else:
                rejections.append(rejection)  # type: ignore[arg-type]

        return IngestResult(
            file_name=path.name,
            source_name=source.name,
            status=STATUS_OK,
            detail="",
            size_bytes=read_result.size_bytes,
            candidates=candidates,
            rejections=rejections,
        )

    def _reuse_entry(self, old_entry: dict, current: FileReadResult) -> IngestResult:
        return IngestResult(
            file_name=current.file_name,
            source_name=old_entry.get("source_name"),
            status=current.status,
            detail=(
                current.detail
                + "；该文件历史上已成功处理，本次沿用历史已入库记录"
            ),
            size_bytes=current.size_bytes,
            candidates=[_candidate_from_state(d) for d in old_entry.get("candidates", [])],
            rejections=[_rejection_from_state(d) for d in old_entry.get("rejections", [])],
            bad_line_no=current.bad_line_no,
            bad_line_preview=current.bad_line_preview,
            reused=True,
        )

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        if not self.input_dir.is_dir():
            raise PipelineError(f"输入目录不存在或不是目录: {self.input_dir}")

        jsonl_files = sorted(
            p for p in self.input_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".jsonl"
        )
        if not jsonl_files:
            raise PipelineError(f"输入目录 {self.input_dir} 中没有 .jsonl 文件")

        state = self._load_state()
        old_files: dict = state["files"]
        new_files: dict = {}
        ingest_results: list[IngestResult] = []

        for path in jsonl_files:
            source = self.config.source_for_file(path.name)
            old_entry = old_files.get(path.name)
            fingerprint = _fingerprint(path)

            # 续跑捷径：文件未变化且历史成功，直接复用，绝不重复写入（需求 6）
            if old_entry and old_entry.get("status") == STATUS_OK \
                    and fingerprint is not None \
                    and old_entry.get("fingerprint") == fingerprint:
                result = IngestResult(
                    file_name=path.name,
                    source_name=old_entry.get("source_name"),
                    status=STATUS_OK,
                    detail="",
                    size_bytes=old_entry.get("size_bytes", 0),
                    candidates=[
                        _candidate_from_state(d)
                        for d in old_entry.get("candidates", [])
                    ],
                    rejections=[
                        _rejection_from_state(d)
                        for d in old_entry.get("rejections", [])
                    ],
                    reused=True,
                )
            else:
                result = self._ingest_file(path, source, old_entry)

            ingest_results.append(result)

            if result.status == STATUS_OK:
                if fingerprint is None:
                    fingerprint = _fingerprint(path)
                new_files[path.name] = {
                    "status": STATUS_OK,
                    "source_name": result.source_name,
                    "fingerprint": fingerprint,
                    "size_bytes": result.size_bytes,
                    "candidates": [_candidate_to_state(c) for c in result.candidates],
                    "rejections": [_rejection_to_state(r) for r in result.rejections],
                }
            elif old_entry and old_entry.get("status") == STATUS_OK and result.reused:
                # 历史成功、当前不可读：状态保留为 ok，读取异常放进 read_issue
                entry = json.loads(json.dumps(old_entry))
                entry["read_issue"] = {
                    "status": result.status,
                    "detail": result.detail,
                    "bad_line_no": result.bad_line_no,
                    "bad_line_preview": result.bad_line_preview,
                }
                new_files[path.name] = entry
            else:
                new_files[path.name] = {
                    "status": result.status,
                    "source_name": result.source_name,
                    "size_bytes": result.size_bytes,
                    "detail": result.detail,
                    "bad_line_no": result.bad_line_no,
                    "bad_line_preview": result.bad_line_preview,
                    "candidates": [],
                    "rejections": [],
                }

        # 磁盘上已消失但历史处理过的文件也保留状态，报告中标记 missing
        for name, entry in old_files.items():
            if name not in new_files:
                kept = json.loads(json.dumps(entry))
                kept["read_issue"] = {
                    "status": STATUS_MISSING,
                    "detail": "文件已从输入目录消失，沿用历史已入库记录",
                }
                new_files[name] = kept

        state["files"] = new_files
        state["batch_id"] = self.config.batch_id

        # 全局合并（候选来自所有 ok 文件，含沿用的历史候选）
        all_candidates: list[Candidate] = []
        all_rejections: list[Rejection] = []
        for result in ingest_results:
            all_candidates.extend(result.candidates)
            all_rejections.extend(result.rejections)
        # 已消失文件的历史候选 / 拒绝也要参与重建
        live_names = {r.file_name for r in ingest_results}
        for name, entry in new_files.items():
            if name not in live_names:
                all_candidates.extend(
                    _candidate_from_state(d) for d in entry.get("candidates", [])
                )
                all_rejections.extend(
                    _rejection_from_state(d) for d in entry.get("rejections", [])
                )

        winners, merge_events, conflict_events = self._merge(all_candidates)
        report = self._build_report(
            new_files, ingest_results, winners,
            merge_events, conflict_events, all_rejections,
        )

        # 全部读取 / 校验完成后才落盘，保证错误配置下不产生半成品（需求 9）
        self.output_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(rec, ensure_ascii=False, sort_keys=False)
            for rec in winners["records"]
        ]
        atomic_write_text(self.records_path, ("\n".join(lines) + "\n") if lines else "")
        self._save_state(state)
        return report

    # ---------------------------------------------------------------- merge
    def _merge(self, candidates: list[Candidate]):
        grouped: dict[str, list[Candidate]] = {}
        for c in candidates:
            grouped.setdefault(c.pk, []).append(c)

        winner_records: list[dict] = []
        winner_by_pk: dict[str, Candidate] = {}
        merge_events: list[dict] = []
        conflict_events: list[dict] = []

        for pk in sorted(grouped):
            group = sorted(grouped[pk], key=lambda c: c.rank())
            winner = group[0]
            winner_by_pk[pk] = winner
            winner_records.append(winner.record)

            if len(group) > 1:
                merge_events.append({
                    "type": EVENT_MERGE,
                    "pk": pk,
                    "winner": self._candidate_ref(winner),
                    "losers": [self._candidate_ref(c) for c in group[1:]],
                    "rule": "business_time_latest->source_priority->file->line",
                })

            # 字段冲突：不同来源、同主键、同规范字段、值不同（需求 7）
            by_source: dict[str, Candidate] = {}
            for c in group:
                # 同来源内多条只保留该来源排名最高者参与冲突比对
                if c.source_name not in by_source or \
                        c.rank() < by_source[c.source_name].rank():
                    by_source[c.source_name] = c
            if len(by_source) > 1:
                fields = sorted({f for c in by_source.values() for f in c.record})
                for fname in fields:
                    values = {
                        s: c.record.get(fname)
                        for s, c in by_source.items()
                        if fname in c.record
                    }
                    distinct = {json.dumps(v, ensure_ascii=False, sort_keys=True)
                                for v in values.values()}
                    if len(distinct) > 1:
                        chosen_source = winner.source_name
                        conflict_events.append({
                            "type": EVENT_CONFLICT,
                            "pk": pk,
                            "field": fname,
                            "values": [
                                {
                                    "source": s,
                                    "priority": by_source[s].priority,
                                    "value": c.record.get(fname),
                                    "file": c.file_name,
                                    "line": c.line_no,
                                }
                                for s, c in sorted(
                                    by_source.items(), key=lambda kv: kv[1].priority
                                )
                                if fname in c.record
                            ],
                            "resolved_to": {
                                "source": chosen_source,
                                "value": winner.record.get(fname),
                            },
                        })

        return (
            {"records": winner_records, "by_pk": winner_by_pk},
            merge_events,
            conflict_events,
        )

    @staticmethod
    def _candidate_ref(c: Candidate) -> dict:
        return {
            "source": c.source_name,
            "priority": c.priority,
            "file": c.file_name,
            "line": c.line_no,
            "business_time": c.business_time_str,
        }

    # --------------------------------------------------------------- report
    def _build_report(
        self, file_entries: dict, ingest_results: list[IngestResult],
        winners: dict, merge_events: list[dict], conflict_events: list[dict],
        rejections: list[Rejection],
    ) -> dict:
        event_index: dict[str, dict[str, list]] = {}

        def bucket(file_name: str) -> dict:
            return event_index.setdefault(
                file_name, {"merges": [], "conflicts": [], "rejections": []}
            )

        for ev in merge_events:
            files = {ev["winner"]["file"]}
            files.update(x["file"] for x in ev["losers"])
            for f in files:
                bucket(f)["merges"].append(ev)
        for ev in conflict_events:
            files = {x["file"] for x in ev["values"]}
            for f in files:
                bucket(f)["conflicts"].append(ev)
        for rj in rejections:
            bucket(rj.file_name)["rejections"].append({
                "type": EVENT_REJECTION,
                "pk": rj.pk_display,
                "missing_fields": rj.missing_fields,
                "line": rj.line_no,
            })

        groups = []
        complete_files: list[str] = []
        unfinished_files: list[str] = []

        for file_name in sorted(file_entries):
            entry = file_entries[file_name]
            status = entry["status"]
            ev = event_index.get(file_name, {"merges": [], "conflicts": [], "rejections": []})
            # 只列该文件中“最终赢得主键”的记录，被合并淘汰的候选不在此重复出现
            accepted_pks = sorted({
                c["pk"] for c in entry.get("candidates", [])
                if c["pk"] in winners["by_pk"]
                and winners["by_pk"][c["pk"]].file_name == file_name
            })
            group = {
                "file": file_name,
                "source": entry.get("source_name"),
                "status": status,
                "detail": entry.get("detail", ""),
                "size_bytes": entry.get("size_bytes", 0),
                "bad_line_no": entry.get("bad_line_no"),
                "bad_line_preview": entry.get("bad_line_preview", ""),
                "read_issue": entry.get("read_issue"),
                "accepted_pks": accepted_pks,
                "rejections": sorted(ev["rejections"], key=lambda x: (x["pk"], x["line"])),
                "duplicate_merges": sorted(ev["merges"], key=lambda x: x["pk"]),
                "field_conflicts": sorted(
                    ev["conflicts"], key=lambda x: (x["pk"], x["field"])
                ),
            }
            groups.append(group)
            if is_unfinished(status) or (
                entry.get("read_issue")
                and is_unfinished(entry["read_issue"]["status"])
            ):
                unfinished_files.append(file_name)
            else:
                complete_files.append(file_name)

        unique_conflicts = {json.dumps(e, ensure_ascii=False, sort_keys=True): e
                            for g in groups for e in g["field_conflicts"]}
        unique_merges = {json.dumps(e, ensure_ascii=False, sort_keys=True): e
                         for g in groups for e in g["duplicate_merges"]}

        report = {
            "report_schema": "handover-report/v1",
            "batch_id": self.config.batch_id,
            "generated_at": self.run_at or _now_iso(),
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir),
            "summary": {
                "files_total": len(groups),
                "files_complete": len(complete_files),
                "files_unfinished": len(unfinished_files),
                "input_records_accepted": sum(
                    len(e.get("candidates", [])) for e in file_entries.values()
                ),
                "records_rejected_missing_required": len(rejections),
                "canonical_records": len(winners["records"]),
                "duplicate_merge_events": len(unique_merges),
                "field_conflict_events": len(unique_conflicts),
            },
            "conclusion": {
                # 需求 8：结论分为完整可用与未完成两类
                "complete_usable": sorted(complete_files),
                "unfinished": sorted(unfinished_files),
            },
            "file_groups": groups,
            "events": {
                "duplicate_merges": sorted(unique_merges.values(), key=lambda x: x["pk"]),
                "field_conflicts": sorted(
                    unique_conflicts.values(), key=lambda x: (x["pk"], x["field"])
                ),
            },
        }
        return report


def _now_iso() -> str:
    return format_canonical(datetime.now().astimezone())
