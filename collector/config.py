"""配置读取与校验。

配置为 UTF-8 JSON。必填字段缺失、类型错误或选择器语法无效时，
抛出 :class:`ConfigError`，由入口统一提示并终止，不产出任何文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import soupsieve

_MISSING = object()


class ConfigError(Exception):
    """配置校验失败；``problems`` 为逐条可读的错误说明。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("；".join(problems))


@dataclass(frozen=True)
class ListSelectors:
    row: str
    title: str
    date: str
    link: str


@dataclass(frozen=True)
class DetailSelectors:
    title: str
    date: str


@dataclass(frozen=True)
class PaginationRule:
    next: str
    max_pages: int


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class Settings:
    config_path: Path
    site_root: Path
    list_selectors: ListSelectors
    detail_selectors: DetailSelectors
    pagination: PaginationRule
    sources: tuple[Source, ...]
    timeout_seconds: float
    deadline_seconds: float


def _value(obj: object, dotted: str) -> object:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _string(
    obj: object,
    dotted: str,
    problems: list[str],
    *,
    prefix: str = "",
    required: bool = True,
    default: str | None = None,
) -> str | None:
    path = prefix + dotted
    value = _value(obj, dotted)
    if value is _MISSING:
        if required:
            problems.append(f"缺少必需字段：{path}")
            return None
        return default
    if value is None and not required:
        return default
    if not isinstance(value, str):
        problems.append(f"字段类型错误：{path} 应为字符串")
        return None
    value = value.strip()
    if not value:
        if required:
            problems.append(f"字段不能为空：{path}")
            return None
        return default
    return value


def _selector(obj: object, dotted: str, problems: list[str]) -> str | None:
    selector = _string(obj, dotted, problems)
    if selector is None:
        return None
    try:
        soupsieve.compile(selector)
    except Exception as exc:  # soupsieve.SelectorSyntaxError 及同类错误
        problems.append(
            f"选择器语法无效：{dotted}（{exc.__class__.__name__}: {exc}）"
        )
        return None
    return selector


def _positive_number(
    obj: object,
    dotted: str,
    problems: list[str],
    *,
    default: float | None = None,
    integer: bool = False,
) -> float | int | None:
    value = _value(obj, dotted)
    if value is _MISSING:
        if default is None:
            problems.append(f"缺少必需字段：{dotted}")
            return None
        return int(default) if integer else float(default)
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        kind = "整数" if integer else "数字"
        problems.append(f"字段类型错误：{dotted} 应为{kind}")
        return int(default) if integer else (float(default) if default is not None else None)
    if value <= 0:
        problems.append(f"字段取值无效：{dotted} 必须大于 0")
        return int(default) if integer else (float(default) if default is not None else None)
    return int(value) if integer else float(value)


def load_settings(config_path: Path) -> Settings:
    problems: list[str] = []
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"配置文件不存在：{config_path}"]) from None
    except OSError as exc:
        raise ConfigError([f"无法读取配置文件：{config_path}（{exc.strerror}）"]) from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            [f"配置文件不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列（{exc.msg}）"]
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError(["配置文件顶层必须是 JSON 对象"])

    root_text = _string(raw, "site.root", problems)

    list_row = _selector(raw, "selectors.list.row", problems)
    list_title = _selector(raw, "selectors.list.title", problems)
    list_date = _selector(raw, "selectors.list.date", problems)
    list_link = _selector(raw, "selectors.list.link", problems)
    detail_title = _selector(raw, "selectors.detail.title", problems)
    detail_date = _selector(raw, "selectors.detail.date", problems)

    next_selector = _selector(raw, "pagination.next", problems)
    max_pages = _positive_number(
        raw, "pagination.max_pages", problems, default=None, integer=True
    )
    timeout_seconds = _positive_number(
        raw, "request.timeout_seconds", problems, default=5.0
    )
    deadline_seconds = _positive_number(
        raw, "request.deadline_seconds", problems, default=60.0
    )

    sources: list[Source] = []
    raw_sources = _value(raw, "sources")
    if raw_sources is _MISSING:
        problems.append("缺少必需字段：sources")
    elif not isinstance(raw_sources, list) or not raw_sources:
        problems.append("字段取值无效：sources 必须是非空数组")
    else:
        seen_ids: set[str] = set()
        for index, item in enumerate(raw_sources):
            prefix = f"sources[{index}]."
            if not isinstance(item, dict):
                problems.append(f"字段类型错误：{prefix[:-1]} 应为对象")
                continue
            source_id = _string(item, "id", problems, prefix=prefix)
            name = _string(item, "name", problems, prefix=prefix)
            url = _string(item, "url", problems, prefix=prefix)
            if url is not None:
                parts = urlsplit(url)
                if parts.scheme or parts.netloc:
                    problems.append(
                        f"字段取值无效：{prefix}url 必须是站点内相对路径，"
                        "不允许包含协议或主机名"
                    )
                    url = None
            if source_id is not None:
                if source_id in seen_ids:
                    problems.append(f"字段取值重复：{prefix}id 与其他来源重复")
                seen_ids.add(source_id)
            if source_id and name and url:
                sources.append(Source(id=source_id, name=name, url=url))

    if not sources and isinstance(raw_sources, list) and raw_sources and not problems:
        problems.append("字段取值无效：sources 中没有任何有效来源")

    if problems:
        raise ConfigError(problems)

    site_root = Path(root_text or "").expanduser()
    if not site_root.is_absolute():
        site_root = (config_path.parent / site_root).resolve()
    else:
        site_root = site_root.resolve()

    return Settings(
        config_path=config_path.resolve(),
        site_root=site_root,
        list_selectors=ListSelectors(
            row=list_row, title=list_title, date=list_date, link=list_link
        ),
        detail_selectors=DetailSelectors(title=detail_title, date=detail_date),
        pagination=PaginationRule(next=next_selector, max_pages=int(max_pages or 0)),
        sources=tuple(sources),
        timeout_seconds=float(timeout_seconds),
        deadline_seconds=float(deadline_seconds),
    )
