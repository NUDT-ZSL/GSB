"""将清单、帧序列与配置组合成一次封面选取任务。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .config import Config
from .errors import FrameReadError, SetupError
from .frames import FrameMatch, load_frame_gray, load_frame_image, locate_frame, scan_frames
from .manifest import ManifestEntry
from .report import Report, digest_directory, hash_files
from .scoring import group_duplicates, score_frame

_SLUG_RE = re.compile(r"[^0-9A-Za-z一-鿿._-]+")


def _slug(text: str | None, fallback: str) -> str:
    if not text:
        return fallback
    cleaned = _SLUG_RE.sub("_", text.strip()).strip("_")
    return cleaned[:40] or fallback


def time_tag(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") + "s"


@dataclass
class Candidate:
    entry: ManifestEntry
    match: FrameMatch
    gray: np.ndarray
    score: float
    clarity: float
    contrast: float
    gradient: float
    size_handled: bool
    size_note: str | None
    notes: list[str] = field(default_factory=list)


class SelectionRun:
    """一次封面选取运行。

    ``prepare()`` 完成定位、读取与评分，不写任何文件；
    ``execute()`` 完成重复合并、排序、导出与报告生成。
    """

    def __init__(
        self,
        *,
        frame_dir: Path,
        manifest_path: Path,
        entries: Sequence[ManifestEntry],
        config: Config,
        output_dir: Path,
        iteration: str | None = None,
    ) -> None:
        self.frame_dir = frame_dir
        self.manifest_path = manifest_path
        self.entries = list(entries)
        self.config = config
        self.output_dir = output_dir
        self.iteration = iteration or "default"
        self.candidates: list[Candidate] = []
        self.records: dict[int, dict[str, Any]] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    def _record(
        self,
        entry: ManifestEntry,
        status: str,
        reasons: Sequence[str],
        *,
        output: str | None = None,
        merged_into: str | None = None,
        frame: FrameMatch | None = None,
        score: float | None = None,
        size_note: str | None = None,
    ) -> None:
        self.records[entry.position] = {
            "position": entry.position,
            "label": entry.label,
            "time": entry.time,
            "status": status,
            "output": output,
            "merged_into": merged_into,
            "frame_index": frame.index if frame else None,
            "frame_file": frame.path.name if frame else None,
            "score": score,
            "size_note": size_note,
            "reasons": [r for r in reasons if r],
        }

    def _fail(self, entry: ManifestEntry, reason: str, status: str = "error") -> None:
        self._record(entry, status, [reason])
        self.notes.append(f"{entry.display}：{reason}")

    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """扫描帧目录并处理每条清单记录，不写任何文件。"""

        try:
            index = scan_frames(self.frame_dir)
        except SetupError as exc:
            for entry in self.entries:
                self._fail(entry, str(exc))
            self.notes.insert(0, f"帧序列不可用：{exc}")
            return

        total_files = sum(len(paths) for paths in index.values())
        self.notes.insert(
            0,
            f"帧序列：{total_files} 个图片文件，识别为 {len(index)} 个帧号"
            + (f"（{min(index)}-{max(index)}）" if index else ""),
        )

        for entry in self.entries:
            if not entry.ok:
                self._record(entry, "skipped", [entry.error or "清单条目无效"])
                self.notes.append(f"{entry.display}：已跳过，{entry.error}")
                continue

            assert entry.time is not None
            match, location_notes = locate_frame(entry.time, self.config, index)
            if match is None:
                self._fail(entry, location_notes[0])
                continue

            try:
                gray, size_notes, resized = load_frame_gray(match.path, self.config)
            except FrameReadError as exc:
                self._fail(entry, f"第 {match.index} 帧读取失败：{exc}")
                continue

            metrics = score_frame(gray)
            self.candidates.append(
                Candidate(
                    entry=entry,
                    match=match,
                    gray=gray,
                    score=metrics["score"],
                    clarity=metrics["clarity"],
                    contrast=metrics["contrast"],
                    gradient=metrics["gradient"],
                    size_handled=resized,
                    size_note=size_notes[0] if resized and size_notes else None,
                    notes=list(size_notes) + list(location_notes),
                )
            )

    # ------------------------------------------------------------------
    def _output_name(self, rank: int, candidate: Candidate) -> str:
        tag = _slug(candidate.entry.label, f"entry_{candidate.entry.position:02d}")
        return f"cover_{rank:02d}_{tag}_{time_tag(candidate.entry.time or 0.0)}.png"

    def execute(self, *, started_at: datetime | None = None) -> Report:
        started_at = started_at or datetime.now()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        previous_covers = {
            p.name for p in self.output_dir.glob("cover_*.png") if p.is_file()
        }

        groups = group_duplicates(
            [
                {
                    "position": c.entry.position,
                    "time": c.entry.time,
                    "gray": c.gray,
                    "score": c.score,
                }
                for c in self.candidates
            ],
            self.config.dedupe_threshold,
        )
        by_index = {index: c for index, c in enumerate(self.candidates)}
        if not groups:
            self.notes.append("没有成功定位的候选帧，未输出封面候选图")

        covers: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for group in groups:
            rank = len(covers) + 1
            keeper = by_index[group["keeper"]]
            output_name = self._output_name(rank, keeper)
            try:
                load_frame_image(keeper.match.path, self.config).save(
                    self.output_dir / output_name, format="PNG"
                )
            except (OSError, FrameReadError) as exc:
                reason = f"封面写出失败：{exc}"
                self._fail(keeper.entry, reason)
                for member in group["merged"]:
                    self._fail(
                        by_index[member["index"]].entry,
                        f"所属候选组的封面写出失败（{keeper.entry.display}），未产出候选",
                        status="skipped",
                    )
                failures.append({"output": output_name, "reason": str(exc)})
                continue

            rank_reason = f"按画面差异强度排序第 {rank} 位（评分 {keeper.score:.4f}，梯度 {keeper.gradient:.3f}）"
            self._record(
                keeper.entry,
                "selected",
                keeper.notes + [rank_reason],
                output=output_name,
                frame=keeper.match,
                score=keeper.score,
                size_note=keeper.size_note,
            )

            merged_records = []
            for member in group["merged"]:
                other = by_index[member["index"]]
                reason = (
                    f"与时间点 {time_tag(keeper.entry.time or 0.0)}（{keeper.entry.display}）画面近似重复："
                    f"平均像素差 {member['difference']:.4f} ≤ 阈值 {self.config.dedupe_threshold:g}，"
                    f"合并到 {output_name}"
                )
                self._record(
                    other.entry,
                    "merged",
                    other.notes + [reason],
                    output=output_name,
                    merged_into=output_name,
                    frame=other.match,
                    score=other.score,
                    size_note=other.size_note,
                )
                merged_records.append(
                    {
                        "position": other.entry.position,
                        "label": other.entry.label,
                        "time": other.entry.time,
                        "frame_index": other.match.index,
                        "frame_file": other.match.path.name,
                        "difference": round(member["difference"], 6),
                    }
                )

            covers.append(
                {
                    "rank": rank,
                    "position": keeper.entry.position,
                    "label": keeper.entry.label,
                    "time": keeper.entry.time,
                    "output": output_name,
                    "frame_index": keeper.match.index,
                    "frame_file": keeper.match.path.name,
                    "score": keeper.score,
                    "clarity": keeper.clarity,
                    "contrast": keeper.contrast,
                    "gradient": keeper.gradient,
                    "merged": merged_records,
                }
            )

        produced = {cover["output"] for cover in covers}
        stale = sorted(previous_covers - produced)
        if stale:
            # 只提示不复删：输出目录可能被多次运行共用，由使用者决定是否清理。
            self.notes.append(
                "输出目录中存在本次未产出的旧候选图（可能是早前运行遗留，未删除）："
                + "、".join(stale)
            )

        hashes: dict[str, Any] = {}
        try:
            manifest_digest = hash_files([self.manifest_path])[str(self.manifest_path)]
            hashes["manifest"] = {"path": str(self.manifest_path), "sha256": manifest_digest}
            digest, count = digest_directory(self.frame_dir)
            hashes["frames"] = {"dir": str(self.frame_dir), "file_count": count, "sha256": digest}
        except OSError as exc:
            self.notes.append(f"无法计算输入文件摘要：{exc}")

        ordered = [self.records[entry.position] for entry in self.entries]
        return Report(
            entry_records=ordered,
            groups=covers,
            notes=self.notes,
            hashes=hashes,
            config=self.config,
            started_at=started_at,
            finished_at=datetime.now(),
            iteration=self.iteration,
            output_dir=self.output_dir,
            frame_dir=self.frame_dir,
            manifest_path=self.manifest_path,
            failures=failures,
        )
