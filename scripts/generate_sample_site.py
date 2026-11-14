"""确定性生成本地授权站点快照（样例数据）。

生成一套完全固定、可重复重建的本地镜像站点，包含三类「来源」：
  - announcements：2 个目录页，第 2 页由本地服务模拟「暂时不可读」，
    目录页之间存在跨页重复详情链接；
  - news：2 个目录页，详情页均可用，存在跨页重复链接；
  - tenders：2 个目录页，含缺标题、日期非法、详情页不可读、无响应路径、
    缺日期 / 缺链接的目录行等异常。

所有内容由固定常量决定；连续运行生成的目录逐字节一致。
「不可读 / 无响应」由同目录下的 .mirror.json 描述，由采集程序内嵌的
本地 HTTP 服务模拟，不访问公网。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SITE = {
    "announcements": {
        "list_files": [
            ("page-1.html", [
                ("关于2026年度设备采购计划的通知", "2026-09-12", "detail/a1.html"),
                ("关于开展安全生产月活动的通知", "2026-09-08", "detail/a2.html"),
                ("关于调整办公时间的通知", "2026-08-30", "detail/a3.html"),
                # 同页重复
                ("关于2026年度设备采购计划的通知", "2026-09-12", "detail/a1.html"),
            ], "page-2.html"),
            ("page-2.html", [
                ("关于征集年度优秀案例的通知", "2026-08-21", "detail/a14.html"),
                ("关于完善门禁管理的通知", "2026-08-15", "detail/a5.html"),
                # 跨页重复
                ("关于调整办公时间的通知", "2026-08-30", "detail/a3.html"),
            ], None),
        ],
        "details": {
            "a1.html": ("关于2026年度设备采购计划的通知", "2026-09-12", "根据年度工作安排，现发布设备采购计划。"),
            "a2.html": ("关于开展安全生产月活动的通知", "2026-09-08", "定于九月开展安全生产月活动。"),
            "a3.html": ("关于调整办公时间的通知", "2026-08-30", "自九月起调整办公时间。"),
            "a5.html": ("关于完善门禁管理的通知", "2026-08-15", "门禁系统升级安排。"),
            "a14.html": ("关于征集年度优秀案例的通知", "2026-08-21", "现面向各单位征集优秀案例。"),
        },
    },
    "news": {
        "list_files": [
            ("page-1.html", [
                ("中心开展技术交流活动", "2026-09-11", "detail/b1.html"),
                ("新版办公系统上线运行", "2026-09-05", "detail/b2.html"),
                ("志愿服务队走进社区", "2026-08-28", "detail/b3.html"),
            ], "page-2.html"),
            ("page-2.html", [
                # 日期使用斜杠格式，采集后应统一为 YYYY-MM-DD
                ("青年员工培训班顺利举办", "2026/8/19", "detail/b4.html"),
                # 跨页重复
                ("中心开展技术交流活动", "2026-09-11", "detail/b1.html"),
            ], None),
        ],
        "details": {
            "b1.html": ("中心开展技术交流活动", "2026-09-11", "技术交流活动顺利举行。"),
            "b2.html": ("新版办公系统上线运行", "2026-09-05", "办公系统完成升级。"),
            "b3.html": ("志愿服务队走进社区", "2026-08-28", "志愿服务队开展社区服务。"),
            "b4.html": ("青年员工培训班顺利举办", "2026-08-19", "培训班取得良好效果。"),
        },
    },
    "tenders": {
        "list_files": [
            ("page-1.html", [
                ("办公设备采购项目招标公告", "2026-09-10", "detail/c1.html"),
                ("物业服务采购项目招标公告", "2026-09-03", "detail/c2.html"),
                ("安防系统改造项目招标公告", "2026-08-25", "detail/c3.html"),
                # 目录行缺日期：不入清单
                ("网络设备采购项目招标公告", "", "detail/c4.html"),
                # 详情页缺标题：不入清单
                ("消防设施维保项目招标公告", "2026-08-03", "detail/c7.html"),
                # 结构残缺：只有标题，缺日期与链接
                ("UPS电源采购项目", None, None),
            ], "page-2.html"),
            ("page-2.html", [
                ("空调设备采购项目招标公告", "2026-08-11", "detail/c5.html"),
                # 详情页日期非法：不入清单
                ("会议室改造项目招标公告", "2026-08-06", "detail/c6.html"),
                # 详情页不可读：不入清单
                ("绿化养护项目招标公告", "2026-08-01", "detail/c8.html"),
                # 无响应路径：不入清单，但采集必须在限定时限内结束
                ("食堂服务采购项目招标公告", "2026-07-30", "detail/hang.html"),
            ], None),
        ],
        "details": {
            "c1.html": ("办公设备采购项目招标公告", "2026-09-10", "办公设备采购项目已具备招标条件。"),
            "c2.html": ("物业服务采购项目招标公告", "2026-09-03", "物业服务采购项目公开招标。"),
            "c3.html": ("安防系统改造项目招标公告", "2026-08-25", "安防系统改造项目招标。"),
            "c4.html": (None, "2026-08-18", "网络设备采购项目招标。"),
            "c5.html": ("空调设备采购项目招标公告", "2026-08-11", "空调设备采购项目招标。"),
            "c6.html": ("会议室改造项目招标公告", "2026-13-32", "会议室改造项目招标。"),
            "c7.html": (None, "2026-08-03", "消防设施维保项目招标。"),
            "c8.html": ("绿化养护项目招标公告", "2026-08-01", "绿化养护项目招标。"),
        },
    },
}

# 由采集程序内嵌的本地服务读取；路径均为站点根目录下的相对路径。
MIRROR = {
    "unreadable": [
        "/announcements/page-2.html",
        "/tenders/detail/c8.html",
    ],
    "hang": [
        "/tenders/detail/hang.html",
    ],
    "hang_seconds": 60,
}


def entry_cells(title: str, date: str | None, href: str | None) -> str:
    cells = [f'<td class="title"><a href="{href or "#"}">{title}</a></td>']
    if date is not None:
        cells.append(f'<td class="date">{date}</td>')
    if href is not None:
        cells.append(f'<td class="actions"><a class="detail" href="{href}">详情</a></td>')
    return "<tr>" + "".join(cells) + "</tr>"


def render_list(entries, page_no: int, next_href: str | None) -> str:
    rows = "\n".join(entry_cells(*entry) for entry in entries)
    nav = (
        f'<div class="pagination"><a class="next" href="{next_href}">下一页</a></div>'
        if next_href
        else '<div class="pagination"><span class="end">已是最后一页</span></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>目录第 {page_no} 页</title></head>
<body>
<div class="list">
<table class="entry-list">
<thead><tr><th>标题</th><th>发布日期</th><th>操作</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
{nav}
</body>
</html>
"""


def render_detail(title: str | None, date: str | None, body: str) -> str:
    title_html = f'<h1 class="detail-title">{title}</h1>' if title is not None else ""
    date_html = f'<span class="publish-date">{date}</span>' if date is not None else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>详情页</title></head>
<body>
<article class="detail">
{title_html}
{date_html}
<div class="content"><p>{body}</p></div>
</article>
</body>
</html>
"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性生成本地授权站点快照")
    parser.add_argument("--out", required=True, help="站点输出目录")
    parser.add_argument(
        "--force", action="store_true",
        help="目标目录非空时重建（仅删除该目录下本生成器产生的样例文件）",
    )
    args = parser.parse_args()

    root = Path(args.out).resolve()
    if root.exists():
        if not root.is_dir():
            print(f"错误：输出路径已存在且不是目录：{root}", file=sys.stderr)
            return 1
        if any(root.iterdir()):
            if not args.force:
                print(
                    f"错误：输出目录非空，请指定新的空目录或使用 --force：{root}",
                    file=sys.stderr,
                )
                return 1
            for child in root.iterdir():
                if child.is_dir():
                    _remove_tree(child)
                else:
                    child.unlink()
    root.mkdir(parents=True, exist_ok=True)

    for source, spec in SITE.items():
        src_dir = root / source
        (src_dir / "detail").mkdir(parents=True, exist_ok=True)

        for filename, entries, next_href in spec["list_files"]:
            write(src_dir / filename, render_list(entries, 1 if filename == "page-1.html" else 2, next_href))

        for filename, (title, date, body) in spec["details"].items():
            write(src_dir / "detail" / filename, render_detail(title, date, body))

    import json
    write(root / ".mirror.json", json.dumps(MIRROR, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    print(f"样例站点已生成：{root}")
    return 0


def _remove_tree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
