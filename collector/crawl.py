"""单个来源的采集流程：目录页翻页 -> 去重 -> 逐条校验详情页。

数据口径：
  - 清单的标题、发布日期、详情链接均取自目录页；
  - 详情页用于校验条目可用，必需字段为标题与发布日期；
  - 详情页暂不可读 / 缺必需字段的条目被排除并记录原因；
  - 目录页暂不可读时该来源记为未完成，已取得的条目照常入库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from .config import Settings, Source
from .extract import DateParseError, RawEntry, normalize_text, parse_date, parse_detail_page, parse_list_page
from .fetch import FetchError, FetchTimeout, SiteFetcher

REASON_ENTRY_TITLE = "entry_title"
REASON_ENTRY_DATE = "entry_date"
REASON_ENTRY_LINK = "entry_link"
REASON_DETAIL_UNREADABLE = "detail_unreadable"
REASON_DETAIL_TIMEOUT = "detail_timeout"
REASON_DETAIL_TITLE = "detail_title"
REASON_DETAIL_DATE = "detail_date"

REASON_LABELS = {
    REASON_ENTRY_TITLE: "目录条目缺少标题",
    REASON_ENTRY_DATE: "目录条目日期缺失或非法",
    REASON_ENTRY_LINK: "目录条目缺少详情链接",
    REASON_DETAIL_UNREADABLE: "详情页暂时不可读",
    REASON_DETAIL_TIMEOUT: "详情页无响应（超时）",
    REASON_DETAIL_TITLE: "详情页缺少标题",
    REASON_DETAIL_DATE: "详情页日期缺失或非法",
}

SOURCE_REASON_LABELS = {
    "list_unreadable": "目录页暂时不可读",
    "list_timeout": "目录页无响应（超时）",
    "pagination_loop": "分页链接形成循环",
    "pagination_invalid": "分页链接无效",
    "pagination_offsite": "分页链接指向站点之外",
    "page_limit": "达到分页上限，仍有未读取的目录页",
    "list_structure_incomplete": "目录页结构残缺，未解析出可用条目",
    "deadline_exceeded": "总采集时限耗尽，该来源未完成",
    "detail_limit": "详情页访问超出时限，部分条目未校验",
    "entries_excluded": "部分条目无法采集，来源数据不完整",
}


@dataclass(frozen=True)
class Record:
    source_id: str
    title: str
    publish_date: str
    url: str


@dataclass(frozen=True)
class ExcludedEntry:
    url: str | None
    reason: str
    detail: str
    entry_title: str | None = None

    @property
    def reason_label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


@dataclass(frozen=True)
class SourceResult:
    source: Source
    records: list[Record] = field(default_factory=list)
    excluded: list[ExcludedEntry] = field(default_factory=list)
    pages_read: int = 0
    reason: str | None = None
    reason_detail: str | None = None

    @property
    def status(self) -> str:
        """``complete`` / ``partial``（目录读全但有条目被排除）/ ``incomplete``。

        只有全部条目成功采集才可称为完整来源；存在缺失数据的一律不得
        以空结果显示为完整。
        """
        if self.reason is not None:
            return "incomplete"
        return "partial" if self.excluded else "complete"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def effective_reason(self) -> str | None:
        if self.reason is not None:
            return self.reason
        return "entries_excluded" if self.excluded else None

    @property
    def reason_label(self) -> str | None:
        return SOURCE_REASON_LABELS.get(self.effective_reason or "", self.effective_reason)

    @property
    def reason_text(self) -> str | None:
        if self.reason is not None:
            return self.reason_detail
        if self.excluded:
            counts: dict[str, int] = {}
            for item in self.excluded:
                counts[item.reason_label] = counts.get(item.reason_label, 0) + 1
            parts = [f"{label} {count} 条" for label, count in counts.items()]
            return "以下条目未进入清单：" + "；".join(parts) + "（详见 excluded）"
        return None


def _resolve_link(href: str, page_path: str, origin: str) -> str | None:
    """把目录页上的 href 解析为站点内路径；跨站链接返回 None。"""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        if f"{parts.scheme}://{parts.netloc}" != origin:
            return None
        joined = parts.path or "/"
        if parts.query:
            joined += "?" + parts.query
        return joined
    if href.startswith("//"):
        return None
    joined = urljoin(page_path, href)
    resolved = urlsplit(joined)
    path = resolved.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if resolved.query:
        path += "?" + resolved.query
    return path


def collect_source(source: Source, settings: Settings, fetcher: SiteFetcher) -> SourceResult:
    seen: set[str] = set()
    candidates: list[tuple[RawEntry, str, str]] = []
    excluded: list[ExcludedEntry] = []
    pages_read = 0
    parsed_total = 0
    reason: str | None = None
    reason_detail: str | None = None

    current: str | None = source.url if source.url.startswith("/") else "/" + source.url
    visited_pages: set[str] = set()
    next_href: str | None = None

    while current is not None and pages_read < settings.pagination.max_pages:
        if current in visited_pages:
            reason, reason_detail = "pagination_loop", "分页链接形成循环，已停止翻页"
            break
        visited_pages.add(current)
        if fetcher.remaining() <= 0:
            reason = "deadline_exceeded"
            reason_detail = "总采集时限已耗尽，目录页未能读取"
            break

        try:
            page = fetcher.fetch(current)
        except FetchTimeout as exc:
            reason = "list_timeout"
            reason_detail = f"目录页无响应：{current}（{exc}）"
            break
        except FetchError as exc:
            reason = "list_unreadable" if exc.reason != "bad_path" else "pagination_invalid"
            reason_detail = f"目录页无法读取：{current}（{exc.message}）"
            break

        pages_read += 1
        parsed = parse_list_page(
            page.html,
            page_url=page.url,
            selectors=settings.list_selectors,
            next_selector=settings.pagination.next,
        )
        parsed_total += len(parsed.entries)

        for entry in parsed.entries:
            if not entry.title:
                excluded.append(
                    ExcludedEntry(
                        url=None, reason=REASON_ENTRY_TITLE,
                        detail="目录条目缺少标题，无法生成清单记录",
                    )
                )
                continue
            if entry.href is None:
                excluded.append(
                    ExcludedEntry(
                        url=f"{page.url}#row-{entry.position + 1}",
                        reason=REASON_ENTRY_LINK,
                        detail="目录条目缺少详情链接，无法校验详情页",
                        entry_title=entry.title,
                    )
                )
                continue
            resolved = _resolve_link(entry.href, page.url, fetcher.origin)
            if resolved is None:
                excluded.append(
                    ExcludedEntry(
                        url=entry.href, reason=REASON_ENTRY_LINK,
                        detail="详情链接指向站点之外，已拒绝访问（不访问公网）",
                        entry_title=entry.title,
                    )
                )
                continue
            try:
                publish_date = parse_date(entry.date_text)
            except DateParseError as exc:
                excluded.append(
                    ExcludedEntry(
                        url=resolved, reason=REASON_ENTRY_DATE,
                        detail=f"目录条目发布日期无法解析：{exc}",
                        entry_title=entry.title,
                    )
                )
                continue
            if resolved in seen:
                continue  # 重复链接只保留首次出现的记录
            seen.add(resolved)
            candidates.append((entry, resolved, publish_date))

        next_href = parsed.next_href
        if not next_href:
            current = None
            break
        resolved_next = _resolve_link(next_href, page.url, fetcher.origin)
        if resolved_next is None:
            reason = "pagination_offsite"
            reason_detail = f"下一页链接不在站点范围内：{next_href}"
            current = None
            break
        current = resolved_next

    if reason is None and pages_read and parsed_total == 0:
        # 目录页可读但未解析出任何行：通常意味着页面结构残缺。
        # 不能当作「来源为空且完整」输出。
        reason = "list_structure_incomplete"
        reason_detail = f"目录页未解析出任何条目行，页面结构可能残缺或选择器不匹配：{source.url}"

    if reason is None and current is not None and next_href:
        reason = "page_limit"
        reason_detail = (
            f"已达到分页上限 {settings.pagination.max_pages} 页，"
            f"下一页 {next_href} 未读取"
        )

    records: list[Record] = []
    unfinished_details = False
    for entry, detail_path, publish_date in candidates:
        if fetcher.remaining() <= 0:
            unfinished_details = True
            break
        try:
            page = fetcher.fetch(detail_path)
        except FetchTimeout as exc:
            excluded.append(
                ExcludedEntry(url=detail_path, reason=REASON_DETAIL_TIMEOUT,
                              detail=f"详情页无响应：{exc}", entry_title=entry.title)
            )
            continue
        except FetchError as exc:
            excluded.append(
                ExcludedEntry(url=detail_path, reason=REASON_DETAIL_UNREADABLE,
                              detail=exc.message, entry_title=entry.title)
            )
            continue

        fields = parse_detail_page(page.html, selectors=settings.detail_selectors)
        if not fields.title:
            excluded.append(
                ExcludedEntry(url=detail_path, reason=REASON_DETAIL_TITLE,
                              detail="详情页缺少标题字段", entry_title=entry.title)
            )
            continue
        if not fields.date_text:
            excluded.append(
                ExcludedEntry(url=detail_path, reason=REASON_DETAIL_DATE,
                              detail="详情页缺少发布日期字段", entry_title=entry.title)
            )
            continue
        try:
            parse_date(fields.date_text)
        except DateParseError as exc:
            excluded.append(
                ExcludedEntry(url=detail_path, reason=REASON_DETAIL_DATE,
                              detail=f"详情页发布日期无法解析：{exc}", entry_title=entry.title)
            )
            continue

        records.append(
            Record(
                source_id=source.id,
                title=normalize_text(entry.title),
                publish_date=publish_date,
                url=detail_path,
            )
        )

    if reason is None and unfinished_details:
        reason = "detail_limit"
        reason_detail = "总采集时限耗尽，部分详情页未完成校验"

    return SourceResult(
        source=source,
        records=records,
        excluded=excluded,
        pages_read=pages_read,
        reason=reason,
        reason_detail=reason_detail,
    )
