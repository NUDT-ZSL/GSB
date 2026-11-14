"""HTML 抽取：目录页条目与详情页字段。

所有选择器均来自配置；解析使用 ``html.parser``，对残缺标记尽量宽容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

_DATE_RE = re.compile(
    r"^(?P<year>\d{4})\s*[-/.年]\s*(?P<month>\d{1,2})\s*[-/.月]\s*"
    r"(?P<day>\d{1,2})\s*日?$"
)


class DateParseError(ValueError):
    """日期文本无法解析为有效日历日期。"""


def parse_date(text: str) -> str:
    """把常见中文/西文日期写法规范化为 ``YYYY-MM-DD``。"""
    raw = normalize_text(text)
    match = _DATE_RE.match(raw)
    if not match:
        raise DateParseError(f"无法识别的日期格式：{raw!r}")
    try:
        value = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as exc:
        raise DateParseError(f"不是有效日期：{raw!r}（{exc}）") from None
    return value.isoformat()


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.replace(" ", " ").split())


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


@dataclass(frozen=True)
class RawEntry:
    """目录页上的一行原始条目；字段缺失时为空值。"""

    title: str
    date_text: str
    href: str | None
    page_url: str
    position: int


@dataclass(frozen=True)
class ListPage:
    entries: list[RawEntry] = field(default_factory=list)
    next_href: str | None = None


@dataclass(frozen=True)
class DetailFields:
    title: str
    date_text: str


def parse_list_page(html: str, *, page_url: str, selectors, next_selector: str) -> ListPage:
    soup = parse_html(html)
    entries: list[RawEntry] = []
    for position, row in enumerate(soup.select(selectors.row)):
        title_el = row.select_one(selectors.title)
        date_el = row.select_one(selectors.date)
        link_el = row.select_one(selectors.link)
        href = link_el.get("href") if link_el is not None else None
        if href is not None:
            href = href.strip() or None
        entries.append(
            RawEntry(
                title=normalize_text(title_el.get_text() if title_el is not None else None),
                date_text=normalize_text(date_el.get_text() if date_el is not None else None),
                href=href,
                page_url=page_url,
                position=position,
            )
        )
    next_el = soup.select_one(next_selector)
    next_href = None
    if next_el is not None:
        raw = next_el.get("href")
        if raw is not None:
            next_href = raw.strip() or None
    return ListPage(entries=entries, next_href=next_href)


def parse_detail_page(html: str, *, selectors) -> DetailFields:
    soup = parse_html(html)
    title_el = soup.select_one(selectors.title)
    date_el = soup.select_one(selectors.date)
    return DetailFields(
        title=normalize_text(title_el.get_text() if title_el is not None else None),
        date_text=normalize_text(date_el.get_text() if date_el is not None else None),
    )
