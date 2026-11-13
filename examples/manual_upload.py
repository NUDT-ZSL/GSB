# -*- coding: utf-8 -*-
r"""
手工业务操作样例：不依赖审计工具，直接向本地上传服务发送 multipart 请求。
仅限 127.0.0.1 授权教学环境使用。

用法（先启动任一版服务）：
    python -I examples\manual_upload.py --url http://127.0.0.1:8000 normal
    python -I examples\manual_upload.py --url http://127.0.0.1:8000 traversal
    python -I examples\manual_upload.py --url http://127.0.0.1:8000 fake
    python -I examples\manual_upload.py --url http://127.0.0.1:8000 all
"""
import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BOUNDARY = "----manualsample" + secrets.token_hex(8)
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"


def post_file(base_url: str, filename: str, content: bytes):
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{BOUNDARY}--\r\n".encode("ascii")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="上传服务手工操作样例（本机教学）")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("action", choices=["normal", "traversal", "fake", "all"])
    args = parser.parse_args()

    cases = []
    if args.action in ("normal", "all"):
        cases.append(("正常业务上传（真实 PNG 内容 + .png）", "sample_photo.png", PNG_BYTES))
    if args.action in ("traversal", "all"):
        cases.append(("路径遍历尝试（../ 文件名）", "../sample_escaped.png", PNG_BYTES))
    if args.action in ("fake", "all"):
        cases.append(("类型绕过尝试（PHP 内容 + .png 扩展名）", "sample_fake.png",
                      b"<?php echo 'not an image'; ?>\n"))

    for label, filename, content in cases:
        print(f"\n=== {label} ===")
        print(f"提交文件名: {filename}")
        status, payload = post_file(args.url, filename, content)
        print(f"HTTP {status}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if status == 200:
            print(f"服务实际落盘位置: {payload.get('saved_path')}")


if __name__ == "__main__":
    main()
