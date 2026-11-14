#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内部工单系统 - 附件预览接口（本地漏洞版 / 修复版共用代码）

启动方式：
    python app.py --variant vulnerable --port 5000
    python app.py --variant fixed      --port 5001

两个实例都只绑定 127.0.0.1，仅用于本地安全核验。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ATTACHMENT_DIR = (PROJECT_ROOT / "attachments").resolve()

MARKER_NAME = "secret_marker.txt"
MARKER_TOKEN = "P66-LOCAL-MARKER-A7F3C9"


def create_app(variant: str) -> Flask:
    app = Flask(__name__)
    app.config["VARIANT"] = variant

    @app.get("/api/attachment/preview")
    def preview() -> Response:
        """按附件名预览附件。

        漏洞版：直接把 name 拼接到附件目录后面（字符串拼接 / os.path.join），
                未做规范化与目录归属校验，可被 ../ 或绝对路径逃逸出附件目录。
        修复版：先把路径规范化（resolve），再校验其仍位于附件目录之内，否则拒绝。
        """
        name = request.args.get("name", "")
        app.logger.info("variant=%s name=%r", variant, name)

        if not name:
            return jsonify({"error": "bad_request", "message": "缺少 name 参数"}), 400

        if variant == "vulnerable":
            # 漏洞点：仅做路径拼接，没有校验最终路径是否仍在 ATTACHMENT_DIR 内。
            target = Path(os.path.join(str(ATTACHMENT_DIR), name))
            return _read_file(target)

        # 修复版：先规范化，再确认规范化后的路径位于附件目录内。
        if "\x00" in name:
            return _forbidden("请求参数包含非法字符")
        try:
            candidate = (ATTACHMENT_DIR / name).resolve(strict=False)
            attachment_root = ATTACHMENT_DIR.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return _forbidden(f"路径无法解析: {exc}")

        try:
            candidate.relative_to(attachment_root)
        except ValueError:
            # 规范化后落在附件目录之外（../、绝对路径、盘符跳转等）。
            return _forbidden("预览路径必须位于附件目录内")

        if candidate == attachment_root:
            return _forbidden("预览参数必须指向附件文件")
        return _read_file(candidate)

    def _forbidden(message: str) -> Response:
        body = jsonify({
            "error": "forbidden",
            "message": message,
            "variant": variant,
        })
        return body, 403

    def _read_file(target: Path) -> Response:
        try:
            if not target.is_file():
                return jsonify({"error": "not_found", "message": "附件不存在"}), 404
            data = target.read_bytes()
        except OSError as exc:
            return jsonify({"error": "io_error", "message": str(exc)}), 500
        # 以文本返回，便于核验响应体特征。
        return Response(data, mimetype="text/plain; charset=utf-8")

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="工单系统附件预览服务（本地核验用）")
    parser.add_argument("--variant", choices=("vulnerable", "fixed"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("拒绝绑定非回环地址：本服务仅用于本地核验。", file=sys.stderr)
        return 2

    app = create_app(args.variant)
    print(
        f"variant={args.variant} attachment_dir={ATTACHMENT_DIR} "
        f"listening=http://{args.host}:{args.port}",
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
