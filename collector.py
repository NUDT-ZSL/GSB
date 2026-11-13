"""本地供应商报价快照采集器（Python 3.10+，仅使用标准库）。

功能概览：
  1. 读取并严格校验 config.json（缺字段/非法字段给出具体错误）；
  2. 仅访问回环地址上的模拟供应商页面，按页面标识抓取 HTML；
  3. 解析商品名与报价，报价统一保留两位小数并以数值写入 CSV；
  4. 单个来源不可访问/结构异常不影响其他来源，该来源列入未完成列表；
  5. 个别商品报价非数值（如 N/A）只跳过该商品；
  6. 暂时不可用的来源按配置重试，恢复后仍可在本轮完成；
  7. 输出 CSV（来源,商品名,报价）、JSON 报告（采集时间/完整/未完成列表，字母序）
     与纯文本日志（每个来源的结果与耗时）；
  8. 全部成功退出码 0，存在未完成来源退出码 1，配置错误退出码 2。

用法：
  python collector.py --config config.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Windows 控制台默认可能是 GBK，统一强制 UTF-8，保证子进程输出与日志可被确定性读取
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG_ERROR = 2

# --------------------------------------------------------------------------- #
# 配置加载与校验
# --------------------------------------------------------------------------- #


class ConfigError(Exception):
    """配置缺失或字段非法。"""


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"缺少必填字段: {context}.{key}")
    return mapping[key]


def _require_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _require(mapping, key, context)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"字段 {context}.{key} 必须是非空字符串，实际为: {value!r}")
    return value.strip()


def _require_dir_path(mapping: dict[str, Any], key: str, context: str) -> str:
    return _require_string(mapping, key, context)


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {config_path}（{exc.msg}，第 {exc.lineno} 行）") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    server = _require(raw, "server", "<root>")
    if not isinstance(server, dict):
        raise ConfigError("字段 <root>.server 必须是对象")
    host = _require_string(server, "host", "server")
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(f"server.host 仅允许回环地址 {LOOPBACK_HOSTS}，实际为: {host!r}")
    port = _require(server, "port", "server")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError(f"server.port 必须是 1-65535 的整数，实际为: {port!r}")

    output = _require(raw, "output", "<root>")
    if not isinstance(output, dict):
        raise ConfigError("字段 <root>.output 必须是对象")
    output_dir = _require_dir_path(output, "dir", "output")
    csv_name = _require_string(output, "csv_file", "output")
    report_name = _require_string(output, "report_file", "output")
    log_name = _require_string(output, "log_file", "output")

    http_cfg = raw.get("http", {})
    if not isinstance(http_cfg, dict):
        raise ConfigError("字段 <root>.http 必须是对象")
    timeout = http_cfg.get("timeout_seconds", 10)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"http.timeout_seconds 必须是正数，实际为: {timeout!r}")
    retries = http_cfg.get("retry_attempts", 3)
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 1:
        raise ConfigError(f"http.retry_attempts 必须是 >=1 的整数，实际为: {retries!r}")
    backoff = http_cfg.get("backoff_seconds", 0.5)
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or backoff < 0:
        raise ConfigError(f"http.backoff_seconds 必须是非负数，实际为: {backoff!r}")

    suppliers = _require(raw, "suppliers", "<root>")
    if not isinstance(suppliers, list) or not suppliers:
        raise ConfigError("字段 <root>.suppliers 必须是非空数组")

    seen_names: set[str] = set()
    seen_pages: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(suppliers):
        context = f"suppliers[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} 必须是对象")
        name = _require_string(item, "name", context)
        page_id = _require_string(item, "page_id", context)
        if name in seen_names:
            raise ConfigError(f"{context}.name 与其他供应商重名: {name!r}")
        if page_id in seen_pages:
            raise ConfigError(f"{context}.page_id 与其他供应商重复: {page_id!r}")
        item_port = item.get("port", port)
        if not isinstance(item_port, int) or isinstance(item_port, bool) or not (1 <= item_port <= 65535):
            raise ConfigError(f"{context}.port 必须是 1-65535 的整数，实际为: {item_port!r}")
        seen_names.add(name)
        seen_pages.add(page_id)
        normalized.append({"name": name, "page_id": page_id, "port": item_port})

    base_dir = config_path.resolve().parent
    return {
        "host": host,
        "suppliers": normalized,
        "output_dir": (base_dir / output_dir).resolve(),
        "csv_path": (base_dir / output_dir / csv_name).resolve(),
        "report_path": (base_dir / output_dir / report_name).resolve(),
        "log_path": (base_dir / output_dir / log_name).resolve(),
        "timeout": float(timeout),
        "retry_attempts": retries,
        "backoff": float(backoff),
    }


# --------------------------------------------------------------------------- #
# 页面抓取（带回退重试，仅回环）
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    ok: bool
    html: str | None
    detail: str


def fetch_page(host: str, port: int, page_id: str, timeout: float,
               retry_attempts: int, backoff: float) -> FetchResult:
    """抓取单个来源页面，失败时按固定间隔重试。

    网络错误与 5xx 都视为暂时不可用并重试；4xx 视为标识不存在，同样重试
    （在测试环境中等价于该来源本轮不可达），最终失败由调用方记入未完成列表。
    """
    url = f"http://{host}:{port}/{page_id}"
    last_detail = "未尝试"
    for attempt in range(1, retry_attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return FetchResult(True, resp.read().decode(charset), f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            last_detail = f"HTTP {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_detail = f"连接失败: {exc}"
        if attempt < retry_attempts:
            time.sleep(backoff)
    return FetchResult(False, None, f"重试 {retry_attempts} 次后仍失败（{last_detail}）")


# --------------------------------------------------------------------------- #
# HTML 解析
# --------------------------------------------------------------------------- #


class ProductPageParser(HTMLParser):
    """提取 ul.products > li.product 下 span.name / span.price 的文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[dict[str, str | None]] = []
        self._in_products = False
        self._products_depth = 0
        self._product_depth = 0
        self._capture: str | None = None  # "name" | "price" | None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag == "ul" and "products" in classes:
            self._in_products = True
            self._products_depth = 1
            return
        if self._in_products:
            if tag == "ul":
                self._products_depth += 1
            elif tag == "li" and "product" in classes and self._product_depth == 0:
                self.products.append({"name": None, "price": None})
                self._product_depth = 1
            elif self._product_depth:
                if tag == "li":
                    self._product_depth += 1
                elif tag == "span" and "name" in classes and self.products:
                    self._capture = "name"
                    self._chunks = []
                elif tag == "span" and "price" in classes and self.products:
                    self._capture = "price"
                    self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == "span":
            text = "".join(self._chunks).strip()
            if self.products:
                self.products[-1][self._capture] = text or None
            self._capture = None
            self._chunks = []
            return
        if self._product_depth and tag == "li":
            self._product_depth -= 1
        if self._in_products and tag == "ul":
            self._products_depth -= 1
            if self._products_depth == 0:
                self._in_products = False

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._chunks.append(data)


class PageStructureError(Exception):
    """页面缺少商品列表容器或任何有效商品。"""


def parse_products(html: str) -> list[tuple[str, float]]:
    """解析页面，返回 (商品名, 报价数值) 列表。

    - 缺少 ul.products 容器或其中没有任何 li.product：抛 PageStructureError，
      整个来源记为未完成，不写入任何商品；
    - 单个商品缺少名称/报价，或报价为非数值（如 N/A）：仅跳过该商品；
    - 全部商品都被跳过时同样视为结构异常（来源未完成）。
    """
    parser = ProductPageParser()
    parser.feed(html)
    if not parser.products:
        raise PageStructureError("页面中未找到 ul.products > li.product 结构")

    records: list[tuple[str, float]] = []
    skipped = 0
    for item in parser.products:
        name = item.get("name")
        raw_price = item.get("price")
        if not name or not raw_price:
            skipped += 1
            continue
        try:
            price = round(float(raw_price), 2)
        except ValueError:
            skipped += 1
            continue
        records.append((name.strip(), price))

    if not records:
        raise PageStructureError(f"页面存在 {len(parser.products)} 个商品条目，但没有任何可解析的有效名称与报价")
    if skipped:
        logging.getLogger("collector").info("跳过 %d 个名称/报价缺失或报价非数值的商品", skipped)
    return records


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def write_csv(rows: list[tuple[str, str, float]], csv_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["来源", "商品名", "报价"])
        for source, name, price in rows:
            writer.writerow([source, name, f"{price:.2f}"])


def write_report(report_path: Path, collected_at: str,
                 completed: list[str], incomplete: list[str]) -> None:
    payload = {
        "collected_at": collected_at,
        "completed_sources": sorted(completed),
        "incomplete_sources": sorted(incomplete),
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("collector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run(config_path: Path) -> int:
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"配置错误: {exc}", flush=True)
        return EXIT_CONFIG_ERROR

    cfg["output_dir"].mkdir(parents=True, exist_ok=True)
    logger = setup_logger(cfg["log_path"])
    logger.info("采集开始，配置文件: %s，来源数量: %d", config_path.resolve(), len(cfg["suppliers"]))

    all_rows: list[tuple[str, str, float]] = []
    completed: list[str] = []
    incomplete: list[str] = []

    for supplier in cfg["suppliers"]:
        name, page_id, port = supplier["name"], supplier["page_id"], supplier["port"]
        started = time.perf_counter()
        logger.info("开始采集来源: %s（页面标识: %s，端口: %s）", name, page_id, port)

        fetched = fetch_page(cfg["host"], port, page_id,
                             cfg["timeout"], cfg["retry_attempts"], cfg["backoff"])
        if not fetched.ok:
            elapsed = time.perf_counter() - started
            incomplete.append(name)
            logger.info("来源结果: %s | 状态: 未完成 | 耗时: %.3f 秒 | 原因: %s",
                        name, elapsed, fetched.detail)
            continue

        try:
            products = parse_products(fetched.html or "")
        except PageStructureError as exc:
            elapsed = time.perf_counter() - started
            incomplete.append(name)
            logger.info("来源结果: %s | 状态: 未完成 | 耗时: %.3f 秒 | 原因: 页面结构异常: %s",
                        name, elapsed, exc)
            continue

        for product_name, price in products:
            all_rows.append((name, product_name, price))
        elapsed = time.perf_counter() - started
        completed.append(name)
        logger.info("来源结果: %s | 状态: 完成 | 耗时: %.3f 秒 | 商品数: %d",
                    name, elapsed, len(products))

    # 确定性排序：先按来源名称、再按商品名，保证重复运行输出一致
    all_rows.sort(key=lambda row: (row[0], row[1]))
    write_csv(all_rows, cfg["csv_path"])

    collected_at = datetime.now(timezone.utc).isoformat()
    write_report(cfg["report_path"], collected_at, completed, incomplete)

    logger.info("采集结束：完整来源 %d 个，未完成来源 %d 个，商品记录 %d 条；CSV: %s；报告: %s",
                len(completed), len(incomplete), len(all_rows),
                cfg["csv_path"], cfg["report_path"])

    if incomplete:
        logger.warning("以下来源本轮未完成: %s", ", ".join(sorted(incomplete)))
        return EXIT_INCOMPLETE
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地供应商报价快照采集器")
    parser.add_argument("--config", type=Path, default=Path("config.json"),
                        help="配置文件路径（默认 config.json）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv).config)


if __name__ == "__main__":
    raise SystemExit(main())
