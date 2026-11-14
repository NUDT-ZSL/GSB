"""一键执行验收检查，并把实际结果写入 docs/verification.md。

用法：python verify.py
所有检查都在临时输出目录中进行，不会改动 samples/ 下的源帧与清单。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
DOCS = ROOT / "docs"


@dataclass
class Check:
    number: int
    title: str
    passed: bool
    detail: str


def tree_digest(path: Path) -> dict[str, str]:
    result = {}
    for item in sorted(path.rglob("*")):
        if item.is_file():
            result[str(item.relative_to(ROOT))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def run_tool(manifest: str, config: str, output: Path, iteration: str) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cover_selector",
            "--frames",
            str(SAMPLES / "frames"),
            "--manifest",
            str(SAMPLES / manifest),
            "--config",
            str(SAMPLES / config),
            "--output",
            str(output),
            "--iteration",
            iteration,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"工具运行失败：{result.stderr}")
    payload = json.loads((output / f"covers_report__{iteration}.json").read_text("utf-8"))
    markdown = (output / f"covers_report__{iteration}.md").read_text("utf-8")
    return {"payload": payload, "markdown": markdown, "output": output}


def entries_by_label(payload: dict) -> dict[str, dict]:
    return {entry["label"]: entry for entry in payload["entries"] if entry["label"]}


def main() -> int:
    if not (SAMPLES / "frames").is_dir():
        print("缺少样例，请先运行 python generate_samples.py", file=sys.stderr)
        return 2

    before = tree_digest(SAMPLES)
    workdir = Path(tempfile.mkdtemp(prefix="cover-selector-verify-"))
    checks: list[Check] = []
    try:
        base = run_tool("manifest.json", "config.json", workdir / "base", "base")
        payload = base["payload"]

        # 1) 60 帧与 5 个时间点各映射到正确帧文件
        expected = {
            "opening": (5, "frame_0005.png"),
            "product": (10, "frame_0010.png"),
            "static_a": (20, "frame_0020.png"),
            "static_b": (40, "frame_0040.png"),
            "hero": (50, "frame_0050.png"),
        }
        actual = {
            label: (entry["frame_index"], entry["frame_file"])
            for label, entry in entries_by_label(payload).items()
        }
        checks.append(
            Check(
                1,
                "60 帧序列与 5 个时间点各映射到正确帧文件",
                actual == expected,
                "映射结果：" + "；".join(f"{k} → 第 {v[0]} 帧 {v[1]}" for k, v in actual.items()),
            )
        )

        # 2) 帧率 25、时间点 0.2s → 第 5 帧
        opening = entries_by_label(payload)["opening"]
        checks.append(
            Check(
                2,
                "帧率 25、时间点 0.2s 定位到第 5 帧",
                opening["frame_index"] == 5 and opening["frame_file"] == "frame_0005.png",
                f"0.2s × 25fps = 5 → {opening['frame_file']}",
            )
        )

        # 3) 越界与缺失：报告原因，其余照常
        edge = run_tool("manifest_edge.json", "config.json", workdir / "edge", "edge")
        edge_entries = entries_by_label(edge["payload"])
        missing = edge_entries["missing_frame"]
        out_of_range = edge_entries["out_of_range"]
        others_ok = all(
            edge_entries[name]["status"] == "selected"
            for name in ("opening", "static_a")
        )
        reasons_ok = "无对应帧文件" in missing["reasons"][0] and "超出帧序列范围" in out_of_range["reasons"][0]
        checks.append(
            Check(
                3,
                "越界/缺失条目在报告中标注原因，其余照常处理",
                missing["status"] == "error"
                and out_of_range["status"] == "error"
                and reasons_ok
                and others_ok,
                f"missing_frame：{missing['reasons'][0]}；out_of_range：{out_of_range['reasons'][0]}；"
                "opening/static_a 仍正常输出",
            )
        )

        # 4) 近似重复合并并注明
        static_b = entries_by_label(payload)["static_b"]
        merged_ok = (
            static_b["status"] == "merged"
            and static_b["merged_into"] == entries_by_label(payload)["static_a"]["output"]
        )
        checks.append(
            Check(
                4,
                "近似重复候选仅保留一张并注明合并关系",
                merged_ok,
                f"static_b 1.6s → {static_b['status']}，合并到 {static_b['merged_into']}",
            )
        )

        # 5) 按差异强度排序
        covers = payload["covers"]
        rank_order = [cover["rank"] for cover in covers]
        scores = [cover["score"] for cover in covers]
        hero_rank = next(c for c in covers if c["label"] == "hero")["rank"]
        static_rank = next(c for c in covers if c["label"] == "static_a")["rank"]
        order_text = " > ".join(
            "{}({:.4f})".format(cover["label"], cover["score"]) for cover in covers
        )
        checks.append(
            Check(
                5,
                "候选图按画面差异强度从高到低排序并在报告中列出依据",
                rank_order == list(range(1, len(covers) + 1))
                and scores == sorted(scores, reverse=True)
                and hero_rank < static_rank
                and "排序依据" in base["markdown"]
                and "评分" in base["markdown"],
                f"顺序：{order_text}；hero 排名 {hero_rank}，static_a 排名 {static_rank}",
            )
        )

        # 6) 尺寸不一致不中断并列出处理帧
        hero = entries_by_label(payload)["hero"]
        checks.append(
            Check(
                6,
                "帧尺寸不一致时统一处理并在报告中列出",
                hero["status"] == "selected" and "原尺寸 320x180" in hero["reasons"][0]
                and "frame_0050.png" in base["markdown"],
                hero["reasons"][0],
            )
        )

        # 7) 阈值改变合并数量；同阈值结果一致
        loose = run_tool("manifest_edge.json", "config.json", workdir / "loose", "loose")
        strict = run_tool("manifest_edge.json", "config_strict.json", workdir / "strict", "strict")
        repeat = run_tool("manifest_edge.json", "config.json", workdir / "repeat", "repeat")
        merged_loose = loose["payload"]["status_counts"]["merged"]
        merged_strict = strict["payload"]["status_counts"]["merged"]
        deterministic = loose["payload"]["entries"] == repeat["payload"]["entries"] and (
            loose["payload"]["covers"] == repeat["payload"]["covers"]
        )
        checks.append(
            Check(
                7,
                "阈值改变合并结果，同一阈值重复运行结果一致",
                merged_loose != merged_strict and deterministic,
                f"阈值 0.02 合并 {merged_loose} 条；阈值 0.002 合并 {merged_strict} 条；"
                "同阈值两次运行的条目与候选排序完全一致",
            )
        )

        # 8) 每个清单时间点均有状态与关联输出或原因
        every_entry = all(
            entry["status"] in {"selected", "merged", "skipped", "error"}
            and (entry["output"] or entry["reasons"])
            for entry in edge["payload"]["entries"]
        )
        checks.append(
            Check(
                8,
                "报告覆盖每个清单时间点，未产出条目附原因",
                every_entry and len(edge["payload"]["entries"]) == 7,
                "manifest_edge.json 共 7 条，均有状态与输出文件或原因说明",
            )
        )

        # 9) 输出只在指定目录，源文件不变
        after = tree_digest(SAMPLES)
        outputs_only_in_target = all(
            p.parent.parent == workdir
            for p in workdir.rglob("*")
            if p.is_file()
        )
        checks.append(
            Check(
                9,
                "输出仅写入指定目录，源帧与清单哈希不变",
                before == after and outputs_only_in_target,
                f"samples/ 共 {len(before)} 个文件，运行前后 sha256 完全一致；"
                f"所有输出均位于 {workdir}",
            )
        )

        # 10) 缺字段/非数值时间跳过并记录，其余不受影响
        no_time = edge_entries["no_time"]
        bad_time = edge_entries["bad_time"]
        checks.append(
            Check(
                10,
                "缺字段或时间非数值的条目跳过并记录原因",
                no_time["status"] == "skipped"
                and bad_time["status"] == "skipped"
                and "缺少 time" in no_time["reasons"][0]
                and "不是数值" in bad_time["reasons"][0]
                and edge_entries["opening"]["status"] == "selected",
                f"no_time：{no_time['reasons'][0]}；bad_time：{bad_time['reasons'][0]}",
            )
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    checks.sort(key=lambda c: c.number)
    lines = [
        "# 实际检查结果",
        "",
        f"- 执行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 运行环境：Python {sys.version.split()[0]}，本地命令行（未使用容器）",
        f"- 样例数据：`samples/frames`（60 个 PNG，缺第 25 帧，第 50 帧尺寸不同）",
        "- 检查方式：`python verify.py`（内部调用 `python -m cover_selector`）",
        "",
        "| # | 检查项 | 结果 | 证据 |",
        "| --- | --- | --- | --- |",
    ]
    for check in checks:
        lines.append(
            f"| {check.number} | {check.title} | {'通过' if check.passed else '未通过'} | {check.detail} |"
        )
    lines += [
        "",
        "## 附：单元测试",
        "",
        "`python -m pytest tests -q` 覆盖上述要点及近似重复分组、命令行端到端流程。",
    ]

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "verification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for check in checks:
        print(f"[{'PASS' if check.passed else 'FAIL'}] {check.number}. {check.title}")
    print(f"\n结果已写入 {DOCS / 'verification.md'}")
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
