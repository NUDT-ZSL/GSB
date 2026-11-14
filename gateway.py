#!/usr/bin/env python3
"""本地文件上传网关模拟程序（仅用于授权的本地加固验证）。

两种模式：
  --mode permissive  放行模式：仅按扩展名/MIME 白名单判断，不校验真实内容，
                      双扩展名、MIME 伪造、脚本文件名均可绕过（模拟加固前缺陷）。
  --mode hardened    加固模式：文件名、扩展名、MIME 与内容魔数、脚本内容全量校验，
                      全部恶意载荷均被拒绝（模拟加固后）。

附加能力：
  --drop-on P01,P02  指定载荷编号在投递时由网关直接断开连接，
                      用于验证检测工具的故障隔离能力。

仅使用 Python 标准库，监听本机回环地址，不访问任何外部目标。
"""
from __future__ import annotations

import argparse
import json
import sys
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows GBK 控制台下保证中文输出不崩：把标准流切到 UTF-8，并对无法编码字符兜底
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".gif")
ALLOWED_MIMES = ("image/jpeg", "image/png", "image/gif")

# 图片魔数 -> 该魔数对应的合法 MIME
MAGIC_MIME = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# 文件名中出现这些脚本扩展名即判恶意（双扩展名也命中）
DANGEROUS_EXTS = (".php", ".phtml", ".php5", ".asp", ".aspx", ".jsp", ".js", ".html", ".htm")
# 内容中出现这些脚本片段即判恶意
SCRIPT_TOKENS = (b"<?php", b"<script", b"<%", b"<%@", b"jsp:")


def parse_multipart(body: bytes, content_type: str) -> dict:
    """解析 multipart/form-data，返回 {字段名: (filename|None, value_bytes)}。"""
    header = "Content-Type: " + content_type + "\n\n"
    msg = BytesParser(policy=default).parsebytes(header.encode("ascii") + body)
    fields: dict[str, tuple[str | None, bytes]] = {}
    if not msg.is_multipart():
        return fields
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        fields[name] = (filename, payload)
    return fields


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "LocalUploadGateway/1.0"

    # 网关自身日志只往 stderr 打，检测结论由检测工具的日志体系记录
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[gateway] " + fmt % args + "\n")

    def _send_json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 命名)
        try:
            self._handle_post()
        except (ValueError, KeyError, UnicodeError) as exc:
            # 畸形请求按拒绝处理，避免连接异常被误判为故障注入
            try:
                self._send_json(400, {"accepted": False,
                                      "reason": f"malformed request: {type(exc).__name__}"})
            except OSError:
                self.close_connection = True

    def _handle_post(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if self.path != self.server.upload_path or "multipart/form-data" not in ctype:
            self._send_json(404, {"accepted": False, "reason": "not found"})
            return

        fields = parse_multipart(body, ctype)
        file_field = self.server.file_field
        if file_field not in fields:
            self._send_json(400, {"accepted": False, "reason": "missing file field"})
            return

        filename, content = fields[file_field]
        filename = filename or ""
        declared_mime = self.headers.get("X-Declared-Mime", "")
        # 载荷编号由检测工具作为普通表单字段携带，用于故障注入，不参与安全判定
        payload_id = fields.get("payload_id", (None, b""))[1].decode("utf-8", "replace")

        # 故障注入：对指定载荷直接断开底层连接，不返回任何 HTTP 响应
        if payload_id and payload_id in self.server.drop_ids:
            sys.stderr.write(f"[gateway] drop connection for payload {payload_id}\n")
            try:
                sock = self.connection
                sock.shutdown(2)  # SHUT_RDWR
            except OSError:
                pass
            finally:
                sock.close()
            self.close_connection = True
            return

        if self.server.mode == "permissive":
            # 缺陷逻辑：只看白名单扩展名与声明 MIME，完全不校验真实内容
            lower = filename.lower()
            ext_ok = lower.endswith(ALLOWED_EXTS)
            mime_ok = declared_mime in ALLOWED_MIMES
            if ext_ok and mime_ok:
                self._send_json(200, {
                    "accepted": True,
                    "reason": "permissive gateway accepted by extension/mime allowlist",
                    "stored_as": "uploads/" + filename,
                })
            else:
                self._send_json(403, {
                    "accepted": False,
                    "reason": "extension or mime not in allowlist",
                })
            return

        # ---- 加固模式：全量校验，任一不通过即拒绝 ----
        checks: list[tuple[bool, str]] = []

        lower = filename.lower()
        dangerous = next((e for e in DANGEROUS_EXTS if e in lower), None)
        checks.append((dangerous is None,
                       "no script-like extension in filename" if dangerous is None
                       else f"dangerous extension token {dangerous} in filename"))

        # 文件名不得包含脚本相关特殊字符
        bad_char = next((c for c in filename if c in ";|`$<>\n\r"), None)
        checks.append((bad_char is None,
                       "filename characters safe" if bad_char is None
                       else f"illegal character {bad_char!r} in filename"))

        ext = next((e for e in ALLOWED_EXTS if lower.endswith(e)), None)
        checks.append((ext is not None,
                       f"extension allowlisted ({ext})" if ext else "extension not allowlisted"))

        magic_mime = next((m for sig, m in MAGIC_MIME if content.startswith(sig)), None)
        checks.append((magic_mime is not None,
                       f"content magic recognized ({magic_mime})" if magic_mime
                       else "content has no recognized image magic bytes"))
        checks.append((magic_mime is not None and magic_mime == declared_mime,
                       f"declared mime {declared_mime} matches content"
                       if magic_mime == declared_mime
                       else f"declared mime {declared_mime} mismatches content {magic_mime}"))

        token = next((t for t in SCRIPT_TOKENS if t in content.lower()), None)
        checks.append((token is None,
                       "no script token in content" if token is None
                       else f"script token {token!r} found in content"))

        failed = [msg for ok, msg in checks if not ok]
        if not failed:
            self._send_json(200, {
                "accepted": True,
                "reason": "hardened gateway accepted after full validation",
                "stored_as": "uploads/" + filename,
            })
        else:
            self._send_json(403, {
                "accepted": False,
                "reason": "hardened validation rejected: " + "; ".join(failed),
            })


def main() -> int:
    ap = argparse.ArgumentParser(description="本地文件上传网关模拟程序")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mode", choices=["permissive", "hardened"], required=True)
    ap.add_argument("--upload-path", default="/upload")
    ap.add_argument("--file-field", default="file")
    ap.add_argument("--drop-on", default="",
                    help="逗号分隔的载荷编号，命中时网关直接断开连接（故障注入）")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.mode = args.mode
    server.upload_path = args.upload_path
    server.file_field = args.file_field
    server.drop_ids = {x.strip() for x in args.drop_on.split(",") if x.strip()}

    print(f"gateway listening on http://{args.host}:{args.port}{args.upload_path} "
          f"mode={args.mode} drop_on={sorted(server.drop_ids) or 'none'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
