"""Flask 兼容层：仅依赖标准库实现样本服务所需的 Flask 子集。

真实 Flask 存在时优先使用 Flask（见 sample_app.py 的导入逻辑）；
在无网络、未安装 Flask 的环境中自动回退到本模块，保证样本服务可离线运行。

实现的子集：
- Flask() 应用对象、@app.route 装饰器（支持 <name> 与 <path:name> 转换器）
- request.args / request.form / request.view_args / request.method / request.path
- jsonify、HTTPException、abort
- 简单的多线程 WSGI 开发服务器（app.run）
"""

from __future__ import annotations

import json as _json
import threading
import urllib.parse
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from socketserver import ThreadingMixIn
from http import HTTPStatus


class HTTPException(Exception):
    """与 flask.HTTPException 行为近似的异常类型。"""

    def __init__(self, code: int, description: str | None = None):
        self.code = code
        try:
            self.name = HTTPStatus(code).phrase
        except ValueError:
            self.name = "Unknown"
        self.description = description or self.name
        super().__init__(self.code, self.description)

    def get_response(self):  # 与 Flask 接口保持一致
        return (
            _json.dumps({"error": self.description}),
            self.code,
            {"Content-Type": "application/json"},
        )


def abort(code: int, description: str | None = None):
    raise HTTPException(code, description)


class MultiDict(dict):
    """支持一 key 多值的简单字典，覆盖 Flask MultiDict 的常用接口。"""

    def __init__(self, pairs):
        super().__init__()
        self._lists: dict[str, list[str]] = {}
        for key, value in pairs:
            self._lists.setdefault(key, []).append(value)
            self.setdefault(key, value)  # 保留首个值，dict 风格访问

    def get(self, key, default=None, type=None):
        if key not in self._lists:
            return default
        value = self._lists[key][0]
        if type is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value

    def getlist(self, key):
        return list(self._lists.get(key, []))


class Request:
    """每次请求构造一个，通过线程局部变量以 flask.request 的方式暴露。"""

    def __init__(self, environ: dict):
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        # wsgiref 已做一次 URL 解码；再做一次以支持完全编码的路径（如 %2e%2e）
        self.path = urllib.parse.unquote(environ.get("PATH_INFO", "/"))
        self.query_string = environ.get("QUERY_STRING", "")
        self.args = MultiDict(
            urllib.parse.parse_qsl(self.query_string, keep_blank_values=True)
        )
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        self._body = raw.decode("utf-8", errors="replace")
        self.form = MultiDict(
            urllib.parse.parse_qsl(self._body, keep_blank_values=True)
        )
        self.view_args: dict = {}

    def get_json(self, silent: bool = False):
        try:
            return _json.loads(self._body)
        except ValueError:
            if silent:
                return None
            raise


_local = threading.local()


class _RequestProxy:
    """代理线程局部 Request，使用方式与 flask.request 一致。"""

    def __getattr__(self, name):
        return getattr(_local.request, name)


request = _RequestProxy()


def jsonify(payload=None, **kwargs):
    if payload is None:
        payload = kwargs
    elif kwargs:
        raise TypeError("jsonify 不支持同时传入 dict 与关键字参数")
    return (
        _json.dumps(payload, ensure_ascii=False),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


_RULE_RE_PARTS = []


def _compile_rule(rule: str):
    """把 /files/<path:filename> 编译成 (正则, [变量名], {变量名: 转换器})。"""
    import re

    names: list[str] = []
    converters: dict[str, str] = {}

    def _replace(match):
        content = match.group(1)
        if ":" in content:
            converter, name = content.split(":", 1)
        else:
            converter, name = "default", content
        names.append(name)
        converters[name] = converter
        if converter == "path":
            return r"(?P<%s>.+)" % name
        return r"(?P<%s>[^/]+)" % name

    pattern = re.sub(r"<([^<>]+)>", _replace, rule)
    return re.compile(r"^%s$" % pattern), names, converters


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # 与 Flask 默认日志行为解耦，保持输出干净
        pass


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class Flask:
    def __init__(self, name: str):
        self.name = name
        self._rules: list[tuple] = []  # (regex, names, converters, methods, view)

    def route(self, rule: str, methods=None):
        regex, names, converters = _compile_rule(rule)
        allowed = {m.upper() for m in (methods or ("GET",))}

        def decorator(view):
            self._rules.append((regex, names, converters, allowed, view))
            return view

        return decorator

    def _dispatch(self, req: Request):
        for regex, _names, converters, allowed, view in self._rules:
            match = regex.match(req.path)
            if not match:
                continue
            if req.method not in allowed and "HEAD" not in allowed:
                raise HTTPException(405)
            req.view_args = {}
            for key, value in match.groupdict().items():
                value = urllib.parse.unquote(value)
                if converters.get(key) == "path":
                    value = value.lstrip("/")
                req.view_args[key] = value
            return view(**req.view_args)
        raise HTTPException(404)

    def wsgi_app(self, environ, start_response):
        req = Request(environ)
        _local.request = req
        try:
            result = self._dispatch(req)
        except HTTPException as exc:
            result = exc.get_response()
        body, status, headers = self._normalize(result)
        if "Content-Length" not in {k for k, _ in headers}:
            headers.append(("Content-Length", str(len(body))))
        start_response(
            "%d %s" % (status, HTTPStatus(status).phrase if status < 600 else "Error"),
            headers,
        )
        return [body]

    @staticmethod
    def _normalize(result):
        if isinstance(result, tuple):
            body = result[0]
            status = result[1] if len(result) > 1 else 200
            headers = list((result[2].items() if len(result) > 2 else []))
        else:
            body, status, headers = result, 200, []
        if not isinstance(body, (bytes, bytearray)):
            body = str(body).encode("utf-8")
        return bytes(body), status, headers

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)

    def run(self, host: str = "127.0.0.1", port: int = 8000):
        server = make_server(host, port, self, server_class=_ThreadingServer,
                             handler_class=_QuietHandler)
        print(" * stdlib-flask shim serving on http://%s:%d" % (host, port),
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
