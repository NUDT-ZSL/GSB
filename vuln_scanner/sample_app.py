"""本地授权的漏洞样本服务（仅用于本机安全检测演练）。

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
    "welcome.txt": "welcome to the vulnerable demo service\n",
    "notes/readme.txt": "notes directory readme\n",
}

_SECRET_FILES = {
    "../etc/passwd": "root:x:0:0:root:/root:/bin/bash\n",
    "..\\etc\\passwd": "root:x:0:0:root:/root:/bin/bash\n",
    "../../etc/shadow": "root:*:19000:0:99999:7:::\n",
    "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\n",
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
    username = request.values.get("username", "") if hasattr(request, "values") \
        else (request.form.get("username") or request.args.get("username", ""))
    password = request.values.get("password", "") if hasattr(request, "values") \
        else (request.form.get("password") or request.args.get("password", ""))

    # 漏洞点：字符串直接拼进“SQL”，不做参数化
    query = "SELECT * FROM users WHERE username='%s' AND password='%s'" % (
        username, password)
    if "' OR '1'='1" in username or "' OR 1=1" in username:
        return jsonify({
            "query": query,
            "marker": SQLI_MARKER,
            "message": "login bypass accepted",
        })
    if _FAKE_USERS.get(username) == password:
        return jsonify({"query": query, "message": "login ok"})
    return jsonify({"query": query, "message": "invalid credentials"}), 401


# ---------------------------------------------------------------- XSS
@app.route("/search")
def search():
    keyword = request.args.get("q", "")
    lang = request.args.get("lang", "")
    # 漏洞点：两个输入均未转义直接拼到 HTML 中（同 URL 不同参数，便于去重验收）
    html = "<html><body><h1>Results for %s%s%s</h1>" \
           "<p lang='%s%s%s'></p></body></html>" % (
        XSS_MARKER, keyword, XSS_MARKER,
        XSS_MARKER, lang, XSS_MARKER)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


_COMMENTS: list[str] = []


@app.route("/comment", methods=["GET", "POST"])
def comment():
    text = request.args.get("text") or request.form.get("text") or ""
    # 漏洞点：存储型/反射型混合，同样原样回显；与 /search 同参数名不同 URL
    _COMMENTS.append(text)
    html = "<div class='comment'>%s%s%s</div>" % (XSS_MARKER, text, XSS_MARKER)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------- 目录遍历
@app.route("/file")
def read_file():
    name = request.args.get("name", "")
    # 漏洞点：未规范化路径，直接按请求名读取“磁盘”
    if name in _SECRET_FILES:
        return jsonify({"name": name, "marker": TRAVERSAL_MARKER,
                        "content": _SECRET_FILES[name]})
    if name in _SAFE_FILES:
        return jsonify({"name": name, "content": _SAFE_FILES[name]})
    return jsonify({"name": name, "error": "file not found"}), 404


@app.route("/files/<path:filename>")
def read_file_path(filename):
    # 无参数名的路径型注入点：扫描器对该条目的 parameter 应为 null
    candidate = "/" + filename
    if filename in _SECRET_FILES or candidate in _SECRET_FILES:
        return jsonify({"name": candidate, "marker": TRAVERSAL_MARKER,
                        "content": _SECRET_FILES.get(filename)
                        or _SECRET_FILES[candidate]})
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
