"""一次性生成器：由“原始样本 vs 修复后样本”产出确定性 unified diff 补丁。

运行后补丁写入 patches/fix_vulnerabilities.patch，本脚本可重复运行。
"""

from __future__ import annotations

import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "vuln_scanner" / "sample_app.py"
PATCH = ROOT / "patches" / "fix_vulnerabilities.patch"

# 修复后的完整样本（修复点：参数化查询、HTML 转义、路径白名单校验）
FIXED_SOURCE = '''"""本地授权的漏洞样本服务（仅用于本机安全检测演练）。

包含三类确定性漏洞，行为对扫描器完全可预测：
- SQL 注入（高）：/login，直接字符串拼接用户名
- 反射型 XSS（中）：/search 与 /comment，输入原样回显
- 目录遍历（低）：/file?name= 与 /files/<path:filename>

另有用于验收的辅助路径：
- /error：始终返回 500（异常状态警告）
- /slow：延迟响应（等待上限/超时验证）
- /health：就绪探针

优先使用真实 Flask；未安装时回退到标准库兼容层，行为保持一致。
"""

from __future__ import annotations

import os
import time
import html
import re

try:  # 有网/已安装环境使用真实 Flask
    from flask import Flask, request, jsonify, abort
    _RUNTIME = "flask"
except ModuleNotFoundError:  # 离线环境回退到标准库实现
    from .flask_compat import Flask, request, jsonify, abort
    _RUNTIME = "stdlib-shim"

app = Flask(__name__)

# 模拟的“数据库”与“文件系统”，保证跨平台、确定性
_FAKE_USERS = {
    "admin": "admin-password",
    "alice": "wonderland",
}

_SAFE_FILES = {
    "welcome.txt": "welcome to the vulnerable demo service\\n",
    "notes/readme.txt": "notes directory readme\\n",
}

_SECRET_FILES = {
    "../etc/passwd": "root:x:0:0:root:/root:/bin/bash\\n",
    "..\\\\etc\\\\passwd": "root:x:0:0:root:/root:/bin/bash\\n",
    "../../etc/shadow": "root:*:19000:0:99999:7:::\\n",
    "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\\n",
}

XSS_MARKER = "<xss-echo>"
SQLI_MARKER = "sql-injection-confirmed"
TRAVERSAL_MARKER = "traversal-secret-confirmed"


@app.route("/health")
def health():
    return jsonify({"status": "ok", "runtime": _RUNTIME})


# ---------------------------------------------------------------- SQL 注入
@app.route("/login", methods=["GET", "POST"])
def login():
    username = request.values.get("username", "") if hasattr(request, "values") \\
        else (request.form.get("username") or request.args.get("username", ""))
    password = request.values.get("password", "") if hasattr(request, "values") \\
        else (request.form.get("password") or request.args.get("password", ""))

    # 修复：参数化查询，输入只作为绑定值，绝不拼接 SQL
    query = "SELECT * FROM users WHERE username = ? AND password = ?"
    stored_password = _FAKE_USERS.get(username)
    if stored_password is not None and stored_password == password:
        return jsonify({"query": query, "message": "login ok"})
    return jsonify({"query": query, "message": "invalid credentials"}), 401


# ---------------------------------------------------------------- XSS
@app.route("/search")
def search():
    keyword = request.args.get("q", "")
    lang = request.args.get("lang", "")
    # 修复：所有回显输入做 HTML 转义
    keyword = html.escape(keyword, quote=True)
    lang = html.escape(lang, quote=True)
    html_page = "<html><body><h1>Results for %s%s%s</h1>" \\
           "<p lang=\\"%s%s%s\\"></p></body></html>" % (
        XSS_MARKER, keyword, XSS_MARKER,
        XSS_MARKER, lang, XSS_MARKER)
    return html_page, 200, {"Content-Type": "text/html; charset=utf-8"}


_COMMENTS: list[str] = []


@app.route("/comment", methods=["GET", "POST"])
def comment():
    text = request.args.get("text") or request.form.get("text") or ""
    # 修复：回显前做 HTML 转义
    text = html.escape(text, quote=True)
    _COMMENTS.append(text)
    html_page = "<div class=\\"comment\\">%s%s%s</div>" % (XSS_MARKER, text, XSS_MARKER)
    return html_page, 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------- 目录遍历
@app.route("/file")
def read_file():
    name = request.args.get("name", "")
    # 修复：拒绝遍历特征，仅允许白名单安全文件
    if ".." in name or name.startswith("/") or "\\\\" in name or name.startswith("~"):
        return jsonify({"name": name, "error": "invalid file name"}), 400
    if name in _SECRET_FILES:
        return jsonify({"name": name, "marker": TRAVERSAL_MARKER,
                        "content": _SECRET_FILES[name]})
    if name in _SAFE_FILES:
        return jsonify({"name": name, "content": _SAFE_FILES[name]})
    return jsonify({"name": name, "error": "file not found"}), 404


@app.route("/files/<path:filename>")
def read_file_path(filename):
    # 修复：规范化后限制在白名单根目录内，拒绝任何路径穿越
    candidate = "/" + filename
    if ".." in filename or "\\\\" in filename or filename.startswith("/") \\
            or re.match(r"^[a-zA-Z]:", filename):
        return jsonify({"name": candidate, "error": "invalid path"}), 400
    if filename in _SAFE_FILES:
        return jsonify({"name": filename, "content": _SAFE_FILES[filename]})
    return jsonify({"name": candidate, "error": "file not found"}), 404


# ---------------------------------------------------------------- 验收辅助
@app.route("/error")
def error_path():
    # 确定性 500，供“异常状态记录警告”验收
    abort(500, "deterministic internal error for scanner warning test")


@app.route("/slow")
def slow():
    delay = request.args.get("delay", "3")
    try:
        delay = max(0.0, float(delay))
    except ValueError:
        delay = 3.0
    time.sleep(delay)
    return jsonify({"slept": delay})


if __name__ == "__main__":
    port = int(os.environ.get("SAMPLE_PORT", "8765"))
    host = os.environ.get("SAMPLE_HOST", "127.0.0.1")
    if _RUNTIME == "flask":
        app.run(host=host, port=port, debug=False)
    else:
        app.run(host=host, port=port)
'''


def main() -> None:
    original = TARGET.read_text(encoding="utf-8").splitlines(keepends=True)
    fixed = FIXED_SOURCE.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original, fixed,
        fromfile="a/vuln_scanner/sample_app.py",
        tofile="b/vuln_scanner/sample_app.py",
        n=3)
    PATCH.parent.mkdir(parents=True, exist_ok=True)
    PATCH.write_text("".join(diff), encoding="utf-8")
    print("patch written:", PATCH)


if __name__ == "__main__":
    main()
