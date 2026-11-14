# -*- coding: utf-8 -*-
"""
lfi_scanner.py —— 本地文件包含(LFI)授权批量体检工具

仅使用 Python 3.10+ 标准库，无需联网、无需第三方包，可直接在 Windows
命令提示符(cmd)下运行。

用途：对授权范围内、仅在本机运行的内部 Web 应用做 LFI 批量体检。
对清单中的每个 URL，以常见文件包含参数 + 目录遍历载荷尝试读取系统
敏感文件，并依据响应中的“敏感文件特征串”判断是否疑似存在 LFI。

三类结论：
  - 疑似LFI   ：任一请求的响应中命中敏感文件特征串
  - 确认安全  ：服务可正常响应，但所有响应均未命中特征串
  - 未完成    ：连接失败 / 响应超时 / 服务不可用（含重试后仍失败）

用法示例：
  python lfi_scanner.py D:\\app_root url_lists\\demo_urls.txt --output report.json
"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------
# 检测规则配置（均为公开的、通用的 LFI 检测字典内容）
# ----------------------------------------------------------------------

# 常见文件包含参数名
INCLUDE_PARAMS = [
    "file", "filename", "path", "page", "include", "doc",
    "document", "template", "view", "load",
]

# 目录遍历 / 敏感文件读取载荷（同时覆盖 Windows 与类 Unix 系统）
TRAVERSAL_PAYLOADS = [
    r"../../../../../../../../windows/win.ini",
    r"..\..\..\..\..\..\..\..\windows\win.ini",
    "/etc/passwd",
    "../../../../../../../../etc/passwd",
]

# 敏感文件特征串：只有响应中出现这些内容才允许判为“疑似LFI”（验收点8）
SENSITIVE_SIGNATURES = [
    "[fonts]",          # Windows win.ini 特征
    "for 16-bit app support",
    "root:x:0:0:",      # 类 Unix /etc/passwd 特征
    "/bin/bash",
]

# 响应片段最大保留长度，保证证据可读且报告不过于臃肿
MAX_SNIPPET = 200

# 结论常量
VERDICT_SUSPECT = "疑似LFI"
VERDICT_SAFE = "确认安全"
VERDICT_UNFINISHED = "未完成"


def now_iso():
    """返回本地时间的 ISO 8601 时间戳（精确到秒）。"""
    return datetime.now().isoformat(timespec="seconds")


def fail(message):
    """参数/输入错误：打印明确原因并以非零码退出（验收点1）。"""
    print("错误: " + message, file=sys.stderr)
    sys.exit(2)


def validate_url(raw):
    """
    校验 URL 合法性。仅允许 http/https，且必须带主机名。
    合法则原样返回，非法抛出 ValueError。
    """
    candidate = raw.strip()
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 协议: %r" % raw)
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名: %r" % raw)
    return candidate


def load_url_list(list_file):
    """读取 URL 清单：忽略空行与 # 注释行；发现非法 URL 立即报错退出。"""
    path = Path(list_file)
    if not path.is_file():
        fail("URL 清单文件不存在或不是文件: %s" % path)
    urls = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail("URL 清单不是合法的 UTF-8 文本文件: %s" % path)
    for line_no, line in enumerate(text.splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            urls.append(validate_url(raw))
        except ValueError as exc:
            fail("清单第 %d 行 URL 非法 —— %s" % (line_no, exc))
    if not urls:
        fail("URL 清单为空，没有可检测的 URL: %s" % path)
    return urls


def build_probe_url(base_url, param, payload):
    """在基础 URL 上替换/追加文件包含参数，构造探测 URL。"""
    parts = urllib.parse.urlsplit(base_url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for (k, v) in query if k != param]
    query.append((param, payload))
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


class TransportFailure(Exception):
    """传输层失败（连接拒绝 / 超时 / DNS 失败等），用于触发重试与未完成判定。"""


def do_request(url, timeout):
    """
    发起单个 HTTP GET 请求。
    成功：返回 (HTTP状态码, 响应文本)。
    传输失败：抛出 TransportFailure，错误信息为可读中文（验收点9）。
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Authorized-LFI-HealthCheck/1.0",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            status = resp.getcode()
    except (socket.timeout, TimeoutError) as exc:
        raise TransportFailure("请求超时(超过 %s 秒): %s" % (timeout, exc))
    except urllib.error.HTTPError as exc:
        # 服务端有响应（4xx/5xx），属于“可到达”，读取错误页正文参与特征判定
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body.decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransportFailure("请求超时(超过 %s 秒): %s" % (timeout, reason))
        # 连接拒绝 / 无监听 / DNS 失败等均归为服务不可用
        raise TransportFailure("连接失败，服务不可用: %s" % reason)
    except OSError as exc:
        raise TransportFailure("网络错误，服务不可用: %s" % exc)

    return status, body.decode("utf-8", errors="replace")


def shorten(text):
    """压缩空白并截断响应片段，保证片段是适合放进 JSON 的单行证据。"""
    compact = " ".join(text.split())
    if len(compact) > MAX_SNIPPET:
        return compact[:MAX_SNIPPET] + "…(已截断)"
    return compact


def run_probes(url, timeout):
    """
    对单个 URL 执行一轮完整探测（全部参数 × 全部载荷）。
    返回 (结论, 探测记录列表, 命中的特征串列表)。
    传输层异常直接向上抛 TransportFailure，由调用方决定重试。
    """
    records = []
    matched_signatures = []
    first_kept = False
    for param in INCLUDE_PARAMS:
        for payload in TRAVERSAL_PAYLOADS:
            probe_url = build_probe_url(url, param, payload)
            status, text = do_request(probe_url, timeout)
            hits = [sig for sig in SENSITIVE_SIGNATURES if sig in text]

            # 证据保留策略：首个探测、命中特征的探测必须保留响应片段；
            # 其余探测仅保留请求信息，控制报告体积。
            keep_snippet = bool(hits) or not first_kept
            first_kept = True
            record = {
                "param": param,
                "payload": payload,
                "probe_url": probe_url,
                "http_status": status,
                "matched_signatures": hits,
                "response_snippet": shorten(text) if keep_snippet else "",
            }
            records.append(record)
            if hits:
                for sig in hits:
                    if sig not in matched_signatures:
                        matched_signatures.append(sig)

    verdict = VERDICT_SUSPECT if matched_signatures else VERDICT_SAFE
    return verdict, records, matched_signatures


def build_entry(url, verdict, evidence, error="", request_evidence=None,
                matched=None, attempts=None):
    """构造结构统一、字段固定的单条报告条目（证据字段保证非空，验收点3）。"""
    return {
        "url": url,
        "结论": verdict,
        "检测时间": now_iso(),
        "证据": evidence,
        "错误说明": error,
        "命中特征串": matched or [],
        "尝试次数": attempts if attempts is not None else 1,
        "request_evidence": request_evidence or [],
    }


def scan_url(url, timeout, retries, retry_delay):
    """
    检测单个 URL，传输失败时按配置重试（验收点5：暂时不可用可在后续重试中恢复）。
    任何异常都在本函数内消化，绝不抛到循环之外（验收点6、9）。
    """
    max_attempts = retries + 1
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            verdict, records, matched = run_probes(url, timeout)
        except TransportFailure as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                time.sleep(retry_delay)
                continue
            # 重试用尽 -> 未完成，绝不能混入安全/疑似（验收点4）
            evidence = "服务在 %d 次尝试后仍不可用；最后请求参数: %s；%s" % (
                attempt,
                "、".join(INCLUDE_PARAMS[:3]) + " 等",
                last_error,
            )
            return build_entry(
                url, VERDICT_UNFINISHED, evidence,
                error=last_error, attempts=attempt,
            )
        except Exception as exc:  # 兜底：不吞掉任何异常（验收点9）
            last_error = "未预期的异常: %s: %s" % (type(exc).__name__, exc)
            if attempt < max_attempts:
                time.sleep(retry_delay)
                continue
            return build_entry(
                url, VERDICT_UNFINISHED,
                "检测过程发生异常，已重试 %d 次仍失败；%s" % (attempt, last_error),
                error=last_error, attempts=attempt,
            )

        # 本轮得到了服务响应
        used_params = sorted({r["param"] for r in records})
        if verdict == VERDICT_SUSPECT:
            evidence = (
                "参数 %s 的文件包含请求响应中命中敏感文件特征串 %s；"
                "证据片段: %s" % (
                    "、".join(used_params),
                    "、".join(matched),
                    next((r["response_snippet"] for r in records
                          if r["matched_signatures"]), ""),
                )
            )
        else:
            evidence = (
                "已使用参数 %s 共发起 %d 个文件包含请求，服务均正常响应，"
                "所有响应均未命中敏感文件特征串；代表响应片段: %s" % (
                    "、".join(used_params), len(records),
                    records[0]["response_snippet"] if records else "",
                )
            )
        return build_entry(
            url, verdict, evidence,
            request_evidence=records, matched=matched, attempts=attempt,
        )

    # 理论上不可达，仅作防御性兜底
    return build_entry(
        url, VERDICT_UNFINISHED,
        "检测未能完成: " + last_error, error=last_error,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="授权 LFI 批量体检工具（纯标准库，仅用于授权范围内的本机服务）"
    )
    parser.add_argument("app_root", help="被测应用根目录（必须存在的本地目录）")
    parser.add_argument("url_list", help="URL 清单文件（UTF-8 文本，每行一个 URL）")
    parser.add_argument(
        "-o", "--output", default="lfi_report.json",
        help="JSON 报告输出路径（默认: lfi_report.json）",
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=5.0,
        help="单个请求的超时时间，秒（默认: 5）",
    )
    parser.add_argument(
        "-r", "--retries", type=int, default=2,
        help="传输失败后的重试次数（默认: 2，即最多尝试 3 次）",
    )
    parser.add_argument(
        "-d", "--retry-delay", type=float, default=0.5,
        help="每次重试前的等待秒数（默认: 0.5）",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # ---- 验收点1：路径缺失/非法时明确报错并非零退出 ----
    app_root = Path(args.app_root)
    if not app_root.exists() or not app_root.is_dir():
        fail("应用根目录不存在或不是目录: %s" % app_root)

    urls = load_url_list(args.url_list)
    output_path = Path(args.output)
    if output_path.parent and not output_path.parent.exists():
        fail("报告输出目录不存在: %s" % output_path.parent)

    print("应用根目录 : %s" % app_root.resolve())
    print("待检测 URL : %d 个" % len(urls))
    print("超时=%.1fs 重试=%d 次 重试间隔=%.1fs"
          % (args.timeout, args.retries, args.retry_delay))

    # ---- 逐条检测：单条失败绝不影响其他 URL（验收点6） ----
    results = []
    for index, url in enumerate(urls, start=1):
        print("[%d/%d] 检测 %s ..." % (index, len(urls), url))
        entry = scan_url(url, args.timeout, args.retries, args.retry_delay)
        results.append(entry)
        print("        -> %s%s" % (
            entry["结论"],
            ("（" + entry["错误说明"] + "）") if entry["错误说明"] else "",
        ))

    # ---- 汇总区（验收点7、10） ----
    suspect_urls = [e["url"] for e in results if e["结论"] == VERDICT_SUSPECT]
    safe_urls = [e["url"] for e in results if e["结论"] == VERDICT_SAFE]
    unfinished_urls = [e["url"] for e in results
                       if e["结论"] == VERDICT_UNFINISHED]

    report = {
        "报告生成时间": now_iso(),
        "应用根目录": str(app_root.resolve()),
        "检测参数": {
            "超时秒": args.timeout,
            "重试次数": args.retries,
            "重试间隔秒": args.retry_delay,
            "包含参数": INCLUDE_PARAMS,
            "敏感文件特征串": SENSITIVE_SIGNATURES,
        },
        "汇总": {
            "总数": len(results),
            "确认安全数": len(safe_urls),
            "疑似LFI数": len(suspect_urls),
            "未完成数": len(unfinished_urls),
            "疑似LFI_URL清单": suspect_urls,
            "确认安全_URL清单": safe_urls,
            "未完成_URL清单": unfinished_urls,
        },
        "逐条结果": results,
    }

    # 汇总计数与逐条结论必须一致
    assert (report["汇总"]["总数"]
            == len(suspect_urls) + len(safe_urls) + len(unfinished_urls))

    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    print("-" * 60)
    print("报告已生成: %s" % output_path.resolve())
    print("汇总: 总数=%d 确认安全=%d 疑似LFI=%d 未完成=%d"
          % (len(results), len(safe_urls), len(suspect_urls),
             len(unfinished_urls)))
    if unfinished_urls:
        print("需业务跟进的未完成 URL:")
        for item in unfinished_urls:
            print("  - " + item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
