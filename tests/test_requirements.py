"""端到端校验：对确定性样例站点运行采集，核对 9 项要求。

运行：``python -m pytest -q``（或直接 ``python tests/test_requirements.py``）。
仅使用本地生成的样例站点与 127.0.0.1 回环服务，不访问公网。
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CONFIG = ROOT / "config" / "sample.json"
MISSING_PAGINATION_CONFIG = ROOT / "config" / "missing-pagination.json"
GENERATOR = ROOT / "scripts" / "generate_sample_site.py"

sys.path.insert(0, str(ROOT))

from collector.cli import main as cli_main  # noqa: E402


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    text = path.read_bytes().decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


@pytest.fixture(scope="session")
def site(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("site") / "sample-site"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(target)],
        check=True,
        capture_output=True,
    )
    return target


def make_config(tmp_path: Path, site: Path, *, drop: str | None = None) -> Path:
    data = json.loads(SAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["site"]["root"] = str(site).replace("\\", "/")
    if drop == "pagination":
        data.pop("pagination")
    config = tmp_path / f"config-{drop or 'full'}.json"
    config.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def run_cli(config: Path, out: Path) -> tuple[int, str, str]:
    import contextlib

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli_main(["--config", str(config), "--out", str(out)])
    return code, stdout.getvalue(), stderr.getvalue()


# 1. 缺字段时明确提示，不产出清单或报告 ----------------------------------------


def test_missing_pagination_reports_and_writes_nothing(tmp_path):
    site = tmp_path / "site"
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(site)], check=True, capture_output=True)
    config = make_config(tmp_path, site, drop="pagination")
    out = tmp_path / "out"
    code, stdout, stderr = run_cli(config, out)
    assert code == 2
    assert "pagination" in stderr
    assert "未生成清单与报告" in stderr
    assert not (out / "announcements.csv").exists()
    assert not (out / "source-report.json").exists()
    assert "清单条数" not in stdout


# 2 + 3 + 7. 表头 / 日期格式 / 去重 / 排序 / 字节一致 ---------------------------


def test_sample_output_contract(tmp_path, site):
    config = make_config(tmp_path, site)
    out1 = tmp_path / "out1"
    code, stdout, _ = run_cli(config, out1)
    assert code == 4  # 样例中存在未完成来源（公告第 2 页不可读）

    header, rows = read_csv(out1 / "announcements.csv")
    assert header == ["标题", "发布日期", "详情链接"]
    assert rows, "清单不应为空"
    for row in rows:
        assert len(row) == 3
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[1]), row

    links = [row[2] for row in rows]
    assert len(links) == len(set(links)), "详情链接必须唯一"
    # 跨页重复链接只保留一行
    assert links.count("/announcements/detail/a3.html") == 1
    assert links.count("/news/detail/b1.html") == 1
    # 去重后预期值：公告 3 + 新闻 4 + 招标 4
    assert len(rows) == 11

    keys = [(-int(r[1].replace("-", "")), r[0]) for r in rows]
    assert keys == sorted(keys), "应按发布日期降序、同日期标题升序"

    out2 = tmp_path / "out2"
    run_cli(config, out2)
    assert (out1 / "announcements.csv").read_bytes() == (out2 / "announcements.csv").read_bytes()
    assert (out1 / "source-report.json").read_bytes() == (out2 / "source-report.json").read_bytes()


# 4. 详情页缺标题 / 不可读：不入清单，报告中按原因列出 --------------------------


def test_detail_failures_reported(tmp_path, site):
    config = make_config(tmp_path, site)
    out = tmp_path / "out"
    run_cli(config, out)
    _, rows = read_csv(out / "announcements.csv")
    links = {row[2] for row in rows}

    assert "/tenders/detail/c7.html" not in links  # 缺标题
    assert "/tenders/detail/c4.html" not in links  # 目录条目缺日期
    assert "/tenders/detail/c8.html" not in links  # 详情页不可读
    assert "/tenders/detail/hang.html" not in links  # 无响应
    assert "/tenders/detail/c6.html" not in links  # 日期非法

    report = json.loads((out / "source-report.json").read_text(encoding="utf-8"))
    tenders = next(
        s for s in report["complete_sources"] + report["incomplete_sources"]
        if s["id"] == "tenders"
    )
    # 有条目被排除的来源数据不完整，不得出现在 complete_sources 中
    assert tenders["status"] == "partial"
    assert tenders["reason"] == "entries_excluded"
    assert tenders["reason_detail"]
    assert tenders not in report["complete_sources"]
    by_reason: dict[str, set[str]] = {}
    for item in tenders["excluded"]:
        by_reason.setdefault(item["reason"], set()).add(item["url"])
    assert "/tenders/detail/c7.html" in by_reason["detail_title"]
    assert "/tenders/detail/c8.html" in by_reason["detail_unreadable"]
    assert "/tenders/detail/hang.html" in by_reason["detail_timeout"]
    assert "/tenders/detail/c6.html" in by_reason["detail_date"]
    assert "/tenders/detail/c4.html" in by_reason["entry_date"]


# 5 + 6. 目录页不可读：来源未完成，已采集条目保留，不列入完整来源 ------------------


def test_unreadable_list_page_keeps_records_and_marks_incomplete(tmp_path, site):
    config = make_config(tmp_path, site)
    out = tmp_path / "out"
    run_cli(config, out)
    _, rows = read_csv(out / "announcements.csv")
    links = {row[2] for row in rows}

    for a in ("a1", "a2", "a3"):
        assert f"/announcements/detail/{a}.html" in links
    assert "/announcements/detail/a5.html" not in links  # 第 2 页未读取

    report = json.loads((out / "source-report.json").read_text(encoding="utf-8"))
    complete_ids = {s["id"] for s in report["complete_sources"]}
    incomplete_ids = {s["id"] for s in report["incomplete_sources"]}
    assert "announcements" not in complete_ids
    assert "announcements" in incomplete_ids
    announcements = next(s for s in report["incomplete_sources"] if s["id"] == "announcements")
    assert announcements["reason"] == "list_unreadable"
    assert announcements["reason_detail"]
    assert announcements["records"] == 3
    # 其他来源的条目不受影响：新闻来源完整，招标来源仅有个别坏条目
    news = next(s for s in report["complete_sources"] if s["id"] == "news")
    assert news["records"] == 4
    assert news["status"] == "complete"
    tenders = next(s for s in report["incomplete_sources"] if s["id"] == "tenders")
    assert tenders["status"] == "partial"
    assert tenders["records"] == 4


# 8. 存在无响应路径时，采集在限定时限内结束 ------------------------------------


def test_unresponsive_path_bounded(tmp_path, site):
    config = make_config(tmp_path, site)
    out = tmp_path / "out"
    started = time.monotonic()
    code, _, _ = run_cli(config, out)
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"采集耗时 {elapsed:.1f}s 超出时限"
    _, rows = read_csv(out / "announcements.csv")
    assert len(rows) == 11


# 9. 命令行打印的条数与实际产物一致 --------------------------------------------


def test_cli_counts_match_artifacts(tmp_path, site):
    config = make_config(tmp_path, site)
    out = tmp_path / "out"
    code, stdout, _ = run_cli(config, out)
    _, rows = read_csv(out / "announcements.csv")
    report = json.loads((out / "source-report.json").read_text(encoding="utf-8"))
    match_records = re.search(r"清单条数：(\d+)", stdout)
    match_incomplete = re.search(r"未完成来源数：(\d+)", stdout)
    assert match_records and match_incomplete
    assert int(match_records.group(1)) == len(rows)
    assert int(match_incomplete.group(1)) == len(report["incomplete_sources"])
    assert code in (0, 4)


def test_unwritable_output_dir(tmp_path, site):
    config = make_config(tmp_path, site)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    code, _, stderr = run_cli(config, blocker / "out")
    assert code == 3
    assert "不可写" in stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
