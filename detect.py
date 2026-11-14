"""Detection script for the two upload-service defects.

Sends two deterministic, malicious multipart requests to the selected target
and prints a JSON report:

  check 1 -- path traversal:        filename ``../escape.txt``
  check 2 -- disguised executable:  filename ``shell.php.jpg`` (script bytes)

Standard library only (http.client + hand-built multipart bodies), so it runs
on a machine with nothing but Python installed.

Usage:
    python detect.py --target vuln
    python detect.py --target fixed --url http://127.0.0.1:8002 \\
        --base-dir fixed_workdir
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from urllib.parse import urlparse

from common import (
    DISGUISED_CONTENT,
    DISGUISED_FILENAME,
    EXECUTABLE_EXTENSIONS,
    TRAVERSAL_CONTENT,
    TRAVERSAL_FILENAME,
    build_multipart,
)

DEFAULT_PORTS = {"vuln": 8001, "fixed": 8002}
DEFAULT_DIRS = {"vuln": "vuln_workdir", "fixed": "fixed_workdir"}

CHECK_DESCRIPTIONS = {
    "path_traversal": (
        "路径遍历检测：以文件名 ../escape.txt 上传，观察文件是否越出 "
        "uploads 目录（缺陷服务会在其上级目录生成该文件）"
    ),
    "disguised_executable": (
        "伪装可执行文件检测：上传 shell.php.jpg（内容为脚本字节），"
        "观察服务是否仅凭 .jpg 后缀放行并原样保存"
    ),
}


class ServiceUnreachable(Exception):
    pass


def post_upload(
    url_base: str, filename: str, content: bytes, content_type: str, timeout: float
) -> tuple[int, str]:
    """POST one multipart upload; return ``(status_code, response_text)``."""
    parsed = urlparse(url_base)
    body, content_type_header = build_multipart([(filename, content, content_type)])
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        timeout=timeout,
    )
    try:
        connection.request(
            "POST",
            "/upload",
            body=body,
            headers={"Content-Type": content_type_header},
        )
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    except (ConnectionError, OSError) as exc:
        raise ServiceUnreachable(str(exc)) from exc
    finally:
        connection.close()


def _has_executable_extension(filename: str) -> bool:
    tokens = [token.lower() for token in filename.split(".")[1:]]
    return any(token in EXECUTABLE_EXTENSIONS for token in tokens)


def detect_traversal(base_dir: str | None) -> bool:
    """Disk evidence: did escape.txt land above the uploads folder?"""
    if not base_dir:
        return False
    escaped = os.path.join(os.path.abspath(base_dir), "escape.txt")
    uploads_copy = os.path.join(
        os.path.abspath(base_dir), "uploads", "escape.txt"
    )
    return os.path.isfile(escaped) and not os.path.isfile(uploads_copy)


def detect_disguised_file(base_dir: str | None) -> tuple[bool, str | None]:
    """Disk evidence: is a file with an executable extension stored?"""
    if not base_dir:
        return False, None
    uploads = os.path.join(os.path.abspath(base_dir), "uploads")
    if not os.path.isdir(uploads):
        return False, None
    for name in sorted(os.listdir(uploads)):
        if _has_executable_extension(name):
            return True, name
    return False, None


def run_detection(
    target: str, url_base: str, base_dir: str | None, timeout: float
) -> dict:
    report: dict = {
        "target": target,
        "url": url_base,
        "checks": [],
    }

    # --- check 1: path traversal -----------------------------------------
    try:
        status_1, _ = post_upload(
            url_base, TRAVERSAL_FILENAME, TRAVERSAL_CONTENT, "text/plain", timeout
        )
    except ServiceUnreachable as exc:
        return _unreachable_report(target, url_base, exc)

    disk_escape = detect_traversal(base_dir)
    if disk_escape:
        detected_1 = True
        evidence_1 = "在 uploads 上级目录发现 escape.txt"
    elif base_dir is not None:
        # We can inspect the disk and nothing escaped: trust the hard fact.
        detected_1 = False
        evidence_1 = "uploads 上级目录未发现 escape.txt，服务返回状态码 %d" % status_1
    else:
        # No disk access: fall back to the HTTP decision.
        detected_1 = 200 <= status_1 < 300
        evidence_1 = "无法访问服务端目录，依据 HTTP 状态码 %d 判断" % status_1
    report["checks"].append(
        {
            "check": "path_traversal",
            "description": CHECK_DESCRIPTIONS["path_traversal"],
            "request": "POST /upload, filename=%r" % TRAVERSAL_FILENAME,
            "status_code": status_1,
            "detected": detected_1,
            "evidence": evidence_1,
        }
    )

    # --- check 2: disguised executable -----------------------------------
    try:
        status_2, response_text = post_upload(
            url_base,
            DISGUISED_FILENAME,
            DISGUISED_CONTENT,
            "image/jpeg",
            timeout,
        )
    except ServiceUnreachable as exc:
        # Check 1 succeeded, so keep it but report the partial failure honestly.
        report["error"] = "second request failed: %s" % exc
        report["checks"].append(
            {
                "check": "disguised_executable",
                "description": CHECK_DESCRIPTIONS["disguised_executable"],
                "request": "POST /upload, filename=%r" % DISGUISED_FILENAME,
                "status_code": -1,
                "detected": False,
                "evidence": "请求未完成，不作检测结论",
            }
        )
        return report

    disk_bad, bad_name = detect_disguised_file(base_dir)
    if disk_bad:
        detected_2 = True
        evidence_2 = "uploads 中保存了含可执行扩展名的文件: %s" % bad_name
    elif base_dir is not None:
        detected_2 = False
        evidence_2 = "uploads 中未发现可执行扩展名文件，服务返回状态码 %d" % status_2
    else:
        detected_2 = 200 <= status_2 < 300
        evidence_2 = "无法访问服务端目录，依据 HTTP 状态码 %d 判断" % status_2
    report["checks"].append(
        {
            "check": "disguised_executable",
            "description": CHECK_DESCRIPTIONS["disguised_executable"],
            "request": "POST /upload, filename=%r" % DISGUISED_FILENAME,
            "status_code": status_2,
            "detected": detected_2,
            "evidence": evidence_2,
        }
    )
    return report


def _unreachable_report(target: str, url_base: str, exc: Exception) -> dict:
    # No fabricated conclusions: both checks are status -1 / detected false.
    return {
        "target": target,
        "url": url_base,
        "error": "服务不可达，未能完成检测: %s" % exc,
        "checks": [
            {
                "check": "path_traversal",
                "description": CHECK_DESCRIPTIONS["path_traversal"],
                "request": "POST /upload, filename=%r" % TRAVERSAL_FILENAME,
                "status_code": -1,
                "detected": False,
                "evidence": "连接失败，未发送有效请求",
            },
            {
                "check": "disguised_executable",
                "description": CHECK_DESCRIPTIONS["disguised_executable"],
                "request": "POST /upload, filename=%r" % DISGUISED_FILENAME,
                "status_code": -1,
                "detected": False,
                "evidence": "连接失败，未发送有效请求",
            },
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect path-traversal and disguised-executable upload defects"
    )
    parser.add_argument(
        "--target",
        choices=sorted(DEFAULT_PORTS),
        required=True,
        help="which service flavour to test (selects default port/dir)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="service base URL (default http://127.0.0.1:<flavour port>)",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="server working directory for on-disk evidence "
        "(default: flavour workdir; pass empty string to disable disk checks)",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    url_base = args.url or "http://127.0.0.1:%d" % DEFAULT_PORTS[args.target]
    if args.base_dir is None:
        base_dir = DEFAULT_DIRS[args.target]
    elif args.base_dir == "":
        base_dir = None
    else:
        base_dir = args.base_dir

    report = run_detection(args.target, url_base, base_dir, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
