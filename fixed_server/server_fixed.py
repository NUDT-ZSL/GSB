# -*- coding: utf-8 -*-
"""
本地文件上传服务（已修复版本，仅用于授权范围内的本地安全教学）。

监听地址固定为 127.0.0.1，不对外暴露。

相对漏洞版的两处修复：
  1. 路径遍历修复：对 filename 先取 basename，再用 abspath/realpath 规范化，
     最后用 commonpath 强制目标路径必须位于上传目录之内，越界即拒绝。
  2. 类型绕过修复：扩展名白名单之外，再按魔数（magic bytes）校验真实内容，
     扩展名与内容不一致一律拒绝。

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
PORT = int(os.environ.get("PORT", "8001"))
UPLOAD_DIR = os.path.abspath(os.environ.get("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads")))

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


def content_matches_extension(ext: str, content: bytes) -> bool:
    """按魔数判断文件真实内容是否与扩展名一致。"""
    head = content[:16]
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    if ext == ".txt":
        try:
            content.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "FixedUpload/1.0"

    def _send_json(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "fixed-upload", "upload_dir": UPLOAD_DIR})
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

        # ===== 修复 1：路径规范化 + 目录边界强制校验 =====
        # 先剥离任何目录成分（含 ..、反斜杠、盘符/绝对路径），只保留纯文件名
        safe_name = os.path.basename(filename.replace("\\", "/"))
        if not safe_name or safe_name in (".", ".."):
            self._send_json(400, {"error": "invalid_filename"})
            return
        upload_root = os.path.realpath(UPLOAD_DIR)
        target = os.path.realpath(os.path.join(upload_root, safe_name))
        # commonpath 不允许两个路径落在不同盘符/根下
        try:
            inside = os.path.commonpath([upload_root, target]) == upload_root
        except ValueError:
            inside = False
        if not inside:
            self._send_json(400, {"error": "path_traversal_blocked", "submitted_filename": filename})
            return

        # ===== 修复 2：扩展名白名单 + 内容魔数双重校验 =====
        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            self._send_json(400, {"error": "extension_not_allowed", "extension": ext})
            return
        if not content_matches_extension(ext, content):
            self._send_json(
                400,
                {
                    "error": "content_type_mismatch",
                    "extension": ext,
                    "detail": "文件内容魔数与扩展名不一致",
                },
            )
            return

        os.makedirs(upload_root, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(content)

        self._send_json(
            200,
            {
                "status": "saved",
                "submitted_filename": safe_name,
                "saved_path": target,
                "real_path": os.path.realpath(target),
                "bytes": len(content),
            },
        )

    def log_message(self, fmt, *args):
        sys.stderr.write("[fixed] %s - %s\n" % (self.address_string(), fmt % args))


def main():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), UploadHandler)
    print(f"[fixed] 已修复的上传服务已启动: http://{HOST}:{PORT}")
    print(f"[fixed] 上传目录: {UPLOAD_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fixed] 服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
