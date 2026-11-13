# -*- coding: utf-8 -*-
"""
本地文件上传服务（存在漏洞版本，仅用于授权范围内的本地安全教学）。

监听地址固定为 127.0.0.1，不对外暴露。

故意保留的两个漏洞（供审计工具验证）：
  1. 路径遍历写入：直接把客户端提交的 filename 拼接到上传目录，
     未做路径规范化与目录边界校验，../ 或绝对路径可写到上传目录之外。
  2. 类型绕过：只按扩展名做白名单校验，完全不检查文件真实内容，
     把脚本/可执行内容改名为 .png/.jpg 即可被接受并落盘。

仅使用 Python 标准库，Python 3.10+ 可运行。
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows 控制台默认可能是 GBK 代码页，统一切到 UTF-8，避免中文输出乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
# 上传目录可由环境变量覆盖，便于演示自定义目录场景
UPLOAD_DIR = os.path.abspath(os.environ.get("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads")))

# 仅做“扩展名白名单”，这是漏洞 2 的根因：不校验内容
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".txt"}

MAX_BODY = 10 * 1024 * 1024  # 10 MiB


def parse_multipart(raw: bytes, content_type: str):
    """极简 multipart/form-data 解析器（标准库实现，不依赖已移除的 cgi 模块）。"""
    match = re.search(r"boundary=(\"?)([^\";]+)\1", content_type or "")
    if not match:
        raise ValueError("缺少 multipart boundary")
    boundary = match.group(2).encode("ascii")
    segments = raw.split(b"--" + boundary)
    files = []
    for segment in segments:
        # 跳过前导片段与结束片段
        if not segment.startswith(b"\r\n"):
            continue
        segment = segment[2:]
        if b"\r\n\r\n" not in segment:
            continue
        head_bytes, content = segment.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        head_text = head_bytes.decode("utf-8", "replace")
        headers = {}
        for line in head_text.split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = re.search(r'name="([^"]*)"', disposition)
        filename = re.search(r'filename="([^"]*)"', disposition)
        if filename:
            files.append(
                (
                    name.group(1) if name else "file",
                    filename.group(1),
                    content,
                )
            )
    return files


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "VulnUpload/1.0"

    def _send_json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "vuln-upload", "upload_dir": UPLOAD_DIR})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/upload":
            self._send_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "bad_content_length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"error": "bad_content_length"})
            return
        raw = self.rfile.read(length)
        try:
            files = parse_multipart(raw, self.headers.get("Content-Type", ""))
        except ValueError as exc:
            self._send_json(400, {"error": "bad_multipart", "detail": str(exc)})
            return

        if not files:
            self._send_json(400, {"error": "no_file_field"})
            return

        _, filename, content = files[0]
        if not filename:
            self._send_json(400, {"error": "empty_filename"})
            return

        # ===== 漏洞 2：只看扩展名，不看内容 =====
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            self._send_json(400, {"error": "extension_not_allowed", "extension": ext})
            return

        # ===== 漏洞 1：未规范化路径、未校验目录边界，直接拼接 =====
        destination = os.path.join(UPLOAD_DIR, filename)
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(destination, "wb") as fh:
            fh.write(content)

        self._send_json(
            200,
            {
                "status": "saved",
                "submitted_filename": filename,
                "saved_path": os.path.abspath(destination),
                "real_path": os.path.realpath(destination),
                "bytes": len(content),
            },
        )

    def log_message(self, fmt, *args):  # 精简控制台日志
        sys.stderr.write("[vuln] %s - %s\n" % (self.address_string(), fmt % args))


def main():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), UploadHandler)
    print(f"[vuln] 存在漏洞的上传服务已启动: http://{HOST}:{PORT}")
    print(f"[vuln] 上传目录: {UPLOAD_DIR}")
    print("[vuln] 警告：该版本含路径遍历与类型绕过漏洞，仅限本机教学使用。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[vuln] 服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
