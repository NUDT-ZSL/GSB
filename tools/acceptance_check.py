"""Acceptance demonstration for the lightmap seam checker.

Runs the checker over the deterministic sample, then over a series of
deliberately broken copies - one per acceptance criterion that needs a
failure - and prints a pass/fail line for each check.  Nothing is
contained or networked; every path is local.

    python tools/acceptance_check.py

Exit status is 0 when every check passes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lightmap_seams.report import run_analysis  # noqa: E402
from lightmap_seams.samples import build_sample_dataset  # noqa: E402

from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Checks:
    def __init__(self) -> None:
        self.failures = 0
        self.total = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.total += 1
        if condition:
            print(f"  PASS  {label}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failures += 1
            print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n{title}")


def entry(report: dict, image: str) -> dict:
    for item in report["lightmaps"]:
        if item["image"] == image:
            return item
    raise AssertionError(image)


def palette_pixels(png: Path) -> tuple[int, int]:
    with Image.open(png) as image:
        colours = image.convert("RGB").getcolors(maxcolors=1_000_000) or []
    red = sum(n for n, c in colours if c == (255, 0, 0))
    blue = sum(n for n, c in colours if c == (0, 0, 255))
    return red, blue


def run(tmp: Path, name: str) -> tuple[Path, Path, Path]:
    """Copy the pristine sample into a fresh subdirectory."""

    input_dir = tmp / name / "input"
    output_dir = tmp / name / "output"
    settings = build_sample_dataset(input_dir)
    return settings, input_dir, output_dir


def main() -> int:
    checks = Checks()
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)

        checks.section("Baseline: deterministic sample")
        settings, input_dir, output_dir = run(tmp, "baseline")
        report = run_analysis(settings, output_dir)
        summary = report["summary"]
        checks.check(
            "10 result JSON parses and carries counters",
            all(
                key in summary
                for key in (
                    "total_lightmaps",
                    "completed_lightmaps",
                    "incomplete_lightmaps",
                    "total_leaks",
                )
            ),
            json.dumps(summary, ensure_ascii=False),
        )
        a = entry(report, "sample_lightmap_a.png")
        checks.check(
            "2  brightness step seam flagged",
            a["leak_count"] == 1
            and [s["seam_id"] for s in a["seams"] if s["classification"] == "leak"]
            == ["leak_center"],
            f"seams={a['total_seams']} leaks={a['leak_count']} "
            f"oob={a['out_of_bounds_count']} dup={a['duplicate_count']}",
        )
        oob = next(s for s in a["seams"] if s["seam_id"] == "out_of_bounds")
        checks.check(
            "3  UV 1.2 is out of bounds, not a leak",
            oob["classification"] == "out_of_bounds"
            and "越界" in oob["reason"]
            and "max_difference" not in oob,
            oob["reason"],
        )
        leaks = next(s for s in a["seams"] if s["seam_id"] == "leak_center")
        checks.check(
            "9  difference == threshold is not a leak",
            entry(report, "sample_lightmap_c.png")["leak_count"] == 0
            and next(
                s
                for s in entry(report, "sample_lightmap_c.png")["seams"]
                if s["seam_id"] == "c_exact_threshold"
            )["max_difference"]
            == 32.0,
            f"leak_center max_difference={leaks['max_difference']}",
        )
        deg = next(s for s in a["seams"] if s["seam_id"] == "degenerate_point")
        checks.check(
            "12 identical endpoints degenerate",
            deg["classification"] == "degenerate" and "退化" in deg["reason"],
            deg["reason"],
        )
        checks.check(
            "8  copied seam counted once",
            a["row_count"] == 6 and a["total_seams"] == 5 and a["duplicate_count"] == 1,
            f"rows={a['row_count']} seams={a['total_seams']} dup={a['duplicate_count']}"
            f" leaks={a['leak_count']}",
        )
        red, blue = palette_pixels(output_dir / a["report_file"])
        checks.check(
            "4  leak red + out-of-bounds blue both present",
            red > 0 and blue > 0,
            f"red_px={red} blue_px={blue}",
        )
        before = {p.name: p.read_bytes() for p in input_dir.iterdir() if p.is_file()}
        checks.check(
            "11 inputs untouched, naming pattern",
            all(
                item["report_file"] == Path(item["image"]).stem + "_seam_report.png"
                for item in report["lightmaps"]
            ),
            ", ".join(item["report_file"] for item in report["lightmaps"]),
        )
        checks.check(
            "14 summary equals detail",
            summary["total_lightmaps"] == len(report["lightmaps"])
            and summary["completed_lightmaps"]
            == sum(1 for i in report["lightmaps"] if i["status"] == "completed")
            and summary["total_leaks"]
            == sum(i["leak_count"] for i in report["lightmaps"]),
            json.dumps(summary),
        )
        for label in ("sample_lightmap_b.png", "sample_lightmap_c.png"):
            item = entry(report, label)
            red, blue = palette_pixels(output_dir / item["report_file"])
            with Image.open(input_dir / label) as source:
                size = source.size
            with Image.open(output_dir / item["report_file"]) as annotated:
                checks.check(
                    f"15 {label} clean annotation matches input size",
                    annotated.size == size and red + blue == 0,
                    f"size={annotated.size} palette_px={red + blue}",
                )
        checks.check(
            "11 input bytes unchanged after run",
            before
            == {p.name: p.read_bytes() for p in input_dir.iterdir() if p.is_file()},
        )

        checks.section("Fault injection")
        # 1 - corrupt image
        settings, input_dir, output_dir = run(tmp, "corrupt")
        (input_dir / "sample_lightmap_a.png").write_bytes(b"not a png" * 40)
        report = run_analysis(settings, output_dir)
        broken = entry(report, "sample_lightmap_a.png")
        others = [entry(report, n) for n in ("sample_lightmap_b.png", "sample_lightmap_c.png")]
        checks.check(
            "1  corrupt lightmap does not stop the rest",
            broken["status"] == "incomplete"
            and "无法解码" in broken["reason"]
            and all(i["status"] == "completed" for i in others),
            broken["reason"],
        )

        # 5 - removed endpoint columns
        settings, input_dir, output_dir = run(tmp, "columns")
        csv_path = input_dir / "sample_lightmap_a_seams.csv"
        rows = csv_path.read_text(encoding="utf-8").splitlines()
        csv_path.write_text(
            "\n".join(",".join(r.split(",")[:3]) for r in rows) + "\n",
            encoding="utf-8",
        )
        report = run_analysis(settings, output_dir)
        item = entry(report, "sample_lightmap_a.png")
        checks.check(
            "5  missing endpoint columns marks incomplete",
            item["status"] == "incomplete" and "列缺失" in item["reason"],
            item["reason"],
        )

        # 6 - missing PNG
        settings, input_dir, output_dir = run(tmp, "missing_png")
        (input_dir / "sample_lightmap_b.png").unlink()
        report = run_analysis(settings, output_dir)
        item = entry(report, "sample_lightmap_b.png")
        checks.check(
            "6  missing PNG -> incomplete, no annotation",
            item["status"] == "incomplete"
            and "光照贴图缺失" in item["reason"]
            and not (output_dir / "sample_lightmap_b_seam_report.png").exists(),
            item["reason"],
        )

        # 13 - missing CSV
        settings, input_dir, output_dir = run(tmp, "missing_csv")
        (input_dir / "sample_lightmap_c_seams.csv").unlink()
        report = run_analysis(settings, output_dir)
        item = entry(report, "sample_lightmap_c.png")
        checks.check(
            "13 missing seam CSV -> incomplete, others continue",
            item["status"] == "incomplete"
            and "接缝清单" in item["reason"]
            and entry(report, "sample_lightmap_a.png")["status"] == "completed",
            item["reason"],
        )

        # 7 - threshold -1
        settings, input_dir, output_dir = run(tmp, "threshold")
        document = json.loads(settings.read_text(encoding="utf-8"))
        document["threshold"] = -1
        settings.write_text(json.dumps(document), encoding="utf-8")
        report = run_analysis(settings, output_dir)
        checks.check(
            "7  threshold -1 -> whole report incomplete",
            report["status"] == "incomplete"
            and "阈值无效" in report["reason"]
            and report["summary"]["completed_lightmaps"] == 0,
            report["reason"],
        )

        checks.section("Result")
        print(f"  {checks.total - checks.failures}/{checks.total} checks passed, "
              f"{checks.failures} failed")
        return 1 if checks.failures else 0


if __name__ == "__main__":
    sys.exit(main())
