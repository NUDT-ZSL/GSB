# -*- coding: utf-8 -*-
"""Playwright 表单驱动：逐条打开本地表单页面、填写非空字段并提交。

分类语义（对应需求第 2/3/5/6/7 项）：
- 页面加载失败 / 必填元素缺失      -> 该行 failed，不提交，后续行继续
- 提交返回 2xx 且含 record_id      -> submitted，交由状态回读
- 提交返回 2xx 但不含 record_id    -> failed（提交失败，不触发回读）
- 提交遇 5xx/网络错误/超时         -> 暂时故障，同一页面重试；耗尽 -> unavailable
- 提交返回其他 4xx                 -> failed（确定性拒绝，重试无意义）
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from playwright.sync_api import sync_playwright

# 暂时故障的提交尝试次数（含首次）
SUBMIT_MAX_ATTEMPTS = 3
# 每次提交等待响应的超时（毫秒）
SUBMIT_TIMEOUT_MS = 10_000
# 页面加载超时（毫秒）
PAGE_LOAD_TIMEOUT_MS = 10_000

REQUIRED_SELECTORS = ("#name", "#email", "#department", "#submit-btn")


@dataclass(frozen=True)
class SubmitResult:
    outcome: str               # "submitted" | "failed" | "unavailable"
    record_id: str | None
    message: str
    attempts: int = 1


class FormSession:
    """复用同一个浏览器实例处理整批记录，每行使用独立 Page 保证故障隔离。

    browser 取值：
      "chromium" 优先 Playwright 自带 Chromium；未安装时自动回退系统 Chrome/Edge
      "chrome"/"msedge" 直接使用系统安装的对应浏览器通道
    """

    FALLBACK_CHANNELS = ("chrome", "msedge")

    def __init__(self, base_url: str, headless: bool = True,
                 browser: str = "chromium") -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._headless = headless
        self._browser = browser
        self._playwright = None
        self._browser_obj = None
        self._browser_name = ""

    def __enter__(self) -> "FormSession":
        self._playwright = sync_playwright().start()
        self._browser_obj, self._browser_name = self._launch()
        return self

    def _launch(self):
        chromium = self._playwright.chromium
        if self._browser in ("chrome", "msedge"):
            browser = chromium.launch(headless=self._headless,
                                      channel=self._browser)
            return browser, f"channel:{self._browser}"

        # 默认：先用 Playwright 自带 Chromium，未安装时回退系统浏览器
        try:
            return chromium.launch(headless=self._headless), "bundled-chromium"
        except Exception as first_exc:
            last_exc = first_exc
            for channel in self.FALLBACK_CHANNELS:
                try:
                    browser = chromium.launch(headless=self._headless,
                                              channel=channel)
                    return browser, f"channel:{channel}"
                except Exception as exc:  # 该通道系统未安装，继续尝试
                    last_exc = exc
            raise RuntimeError(
                "未能启动任何 Chromium 内核浏览器（自带 Chromium 与系统 "
                "Chrome/Edge 均不可用），请先运行 python -m playwright install "
                f"chromium 或安装 Chrome。最后错误: {last_exc}")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser_obj is not None:
                self._browser_obj.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def submit_row(self, fields: dict[str, str]) -> SubmitResult:
        """处理一行：打开页面 -> 校验必填元素 -> 只填非空字段 -> 提交（含暂时故障重试）。"""
        page = self._browser_obj.new_page()
        try:
            # 1) 打开表单页面；加载失败只影响本行
            try:
                page.goto(self._base_url,
                          wait_until="domcontentloaded",
                          timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as exc:  # Playwright 抛错类型较多，统一兜底
                return SubmitResult("failed", None,
                                    f"表单页面加载失败，已取消该行提交: {exc}")

            # 2) 必填元素校验：任一缺失即取消该行，不进行提交
            missing = [sel for sel in REQUIRED_SELECTORS
                       if page.locator(sel).count() == 0]
            if missing:
                return SubmitResult(
                    "failed", None,
                    f"必填页面元素缺失 {missing}，已取消该行提交")

            # 3) 只填写非空字段，空字段保留空白，不写入任何占位内容
            for field_name, value in fields.items():
                page.fill(f"#{field_name}", value)

            # 4) 提交并捕获 /api/submit 响应；暂时故障在同一页面重试
            last_message = "提交未获得响应"
            for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
                try:
                    with page.expect_response(
                        lambda r: "/api/submit" in r.url,
                        timeout=SUBMIT_TIMEOUT_MS,
                    ) as resp_info:
                        page.click("#submit-btn")
                    response = resp_info.value
                    try:
                        body = response.json()
                    except Exception:
                        body = None
                    code = response.status

                    if 200 <= code < 300:
                        record_id = (body or {}).get("record_id")
                        if record_id:
                            return SubmitResult(
                                "submitted", str(record_id),
                                str((body or {}).get("message") or "提交成功"),
                                attempts=attempt)
                        # 2xx 但无记录标识：确定性提交失败，不重试、不回读
                        return SubmitResult(
                            "failed", None,
                            "提交响应未包含记录标识 record_id，记为提交失败",
                            attempts=attempt)
                    if 500 <= code < 600:
                        # 暂时故障：保留恢复机会，重试同一表单
                        last_message = (
                            f"提交接口暂时故障 HTTP {code}: "
                            f"{(body or {}).get('message', '')}")
                        continue
                    # 其他状态码（如 4xx）视为确定性失败
                    return SubmitResult(
                        "failed", None,
                        f"提交被拒绝 HTTP {code}: "
                        f"{(body or {}).get('message', '')}",
                        attempts=attempt)
                except Exception as exc:
                    # 响应超时/网络错误等：同样按暂时故障处理
                    last_message = f"提交请求异常（按暂时故障处理）: {exc}"
                    continue

            return SubmitResult(
                "unavailable", None,
                f"重试 {SUBMIT_MAX_ATTEMPTS} 次后提交仍未恢复（{last_message}）",
                attempts=SUBMIT_MAX_ATTEMPTS)
        finally:
            page.close()


@contextmanager
def open_session(base_url: str, headless: bool = True, browser: str = "chromium"):
    session = FormSession(base_url=base_url, headless=headless, browser=browser)
    with session:
        yield session
