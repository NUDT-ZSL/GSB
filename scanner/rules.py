# -*- coding: utf-8 -*-
"""静态分析规则：污点源、净化函数、危险汇聚点(sink)、修复建议。

严重级别（任务限定枚举）：
    高危 / 中危 / 低危
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 1) 污点源（用户可控输入）。键为“模块.函数”或模块内直接使用的函数名。
#    命中后产生污点等级 RAW(2)：原始未净化输入。
# ---------------------------------------------------------------------------
TAINT_SOURCES: dict[str, str] = {
    "flask.request.form.get": "HTTP 表单字段",
    "flask.request.files": "HTTP 上传文件",
    "flask.request.args.get": "HTTP 查询参数",
    "flask.request.values.get": "HTTP 请求参数",
    "django.http.HttpRequest.FILES": "HTTP 上传文件",
    "django.http.HttpRequest.GET": "HTTP 查询参数",
    "django.http.HttpRequest.POST": "HTTP 表单字段",
    # 任意对象上的 request.FILES / request.form / request.GET ...
    "request.FILES": "HTTP 上传文件",
    "request.files": "HTTP 上传文件",
    "request.form": "HTTP 表单字段",
    "request.GET": "HTTP 查询参数",
    "request.POST": "HTTP 表单字段",
    "request.args": "HTTP 查询参数",
    "request.values": "HTTP 请求参数",
    "request.headers": "HTTP 请求头",
    "request.cookies": "HTTP Cookie",
    # 通用字典/输入读取 API
    "input": "标准输入",
    "sys.stdin.read": "标准输入",
    "os.environ.get": "环境变量",
    "os.getenv": "环境变量",
    "cgi.FieldStorage": "CGI 表单输入",
}

# request 对象的“用户可控字段”名（配合任意对象名 request 使用）
REQUEST_ATTRS: dict[str, str] = {
    "FILES": "HTTP 上传文件",
    "files": "HTTP 上传文件",
    "form": "HTTP 表单字段",
    "GET": "HTTP 查询参数",
    "POST": "HTTP 表单字段",
    "args": "HTTP 查询参数",
    "values": "HTTP 请求参数",
    "headers": "HTTP 请求头",
    "cookies": "HTTP Cookie",
    "data": "HTTP 请求体",
    "json": "HTTP JSON 请求体",
}

# 从 request.FILES["x"].filename / .name 取文件名的属性链
FILENAME_ATTRS = {"filename", "name", "file_name"}

# ---------------------------------------------------------------------------
# 2) 强净化函数：可消除路径穿越污点（等级降为 CLEAN(0)）。
# ---------------------------------------------------------------------------
STRONG_SANITIZERS: dict[str, str] = {
    "os.path.basename": "os.path.basename",
    "posixpath.basename": "posixpath.basename",
    "ntpath.basename": "ntpath.basename",
    "pathlib.PurePath.name": "PurePath.name",
    "pathlib.PurePosixPath.name": "PurePosixPath.name",
    "pathlib.PureWindowsPath.name": "PureWindowsPath.name",
    "werkzeug.utils.secure_filename": "secure_filename",
    "werkzeug.datastructures.FileStorage.filename": "secure_filename",
    "uuid.uuid4.hex": "uuid4().hex",
    "uuid.uuid4.__str__": "str(uuid4())",
    "uuid.uuid1.hex": "uuid1().hex",
}

# ---------------------------------------------------------------------------
# 3) 弱净化函数：黑名单替换/扩展名校验等可被绕过，污点降为 WEAK(1)。
# ---------------------------------------------------------------------------
WEAK_SANITIZERS: dict[str, str] = {
    "str.replace": "str.replace 黑名单替换（可用 ....// 等方式绕过）",
    "str.strip": "str.strip（无法移除路径中的 ../）",
    "str.lstrip": "str.lstrip（无法移除路径中的 ../）",
}

# 仅校验扩展名的方法（配合 endswith/rsplit 等模式识别）
EXTENSION_ONLY_HINTS = ("endswith", "rsplit", "splitext")

# ---------------------------------------------------------------------------
# 4) 危险汇聚点（sink）。
#    kind=write 为文件写入类（高危/中危），其余为读/删类（低危/中危）。
# ---------------------------------------------------------------------------
WRITE_SINKS: dict[str, str] = {
    "builtins.open": "open",
    "io.open": "io.open",
    "os.open": "os.open",
    "pathlib.Path.write_text": "Path.write_text",
    "pathlib.Path.write_bytes": "Path.write_bytes",
    "pathlib.PurePath.joinpath.open": "Path.open",
    "shutil.copyfile": "shutil.copyfile",
    "shutil.copy": "shutil.copy",
    "shutil.copy2": "shutil.copy2",
    "shutil.copyfileobj": "shutil.copyfileobj",
}

# Path 对象上的方法型 sink（调用形态为 <expr>.write_bytes(...) 等）
PATH_WRITE_METHODS = {
    "write_text": "Path.write_text",
    "write_bytes": "Path.write_bytes",
    "open": "Path.open",
}

READ_SINKS: dict[str, str] = {
    "pathlib.Path.read_text": "Path.read_text",
    "pathlib.Path.read_bytes": "Path.read_bytes",
    "shutil.copyfileobj.read": "read",
}

PATH_READ_METHODS = {
    "read_text": "Path.read_text",
    "read_bytes": "Path.read_bytes",
}

DELETE_SINKS: dict[str, str] = {
    "os.remove": "os.remove",
    "os.unlink": "os.unlink",
    "pathlib.Path.unlink": "Path.unlink",
    "shutil.rmtree": "shutil.rmtree",
}

# FileStorage / 文件对象的保存方法
SAVE_METHODS = {"save", "save_as"}

# ---------------------------------------------------------------------------
# 5) 路径约束守卫：函数内若对最终路径做了边界校验，则该函数的穿越告警抑制。
# ---------------------------------------------------------------------------
CONTAINMENT_GUARDS = {
    "os.path.commonpath",
    "pathlib.Path.resolve",
    "pathlib.PurePath.resolve",
    "os.path.realpath",
}
GUARD_CHECK_ATTRS = {"startswith", "is_relative_to", "relative_to"}
GUARD_BASE_NAMES = {"upload_dir", "upload_root", "base_dir", "upload_path"}

# ---------------------------------------------------------------------------
# 6) 污点等级与严重级别
# ---------------------------------------------------------------------------
CLEAN, WEAK, RAW = 0, 1, 2

SEVERITY_HIGH = "高危"
SEVERITY_MEDIUM = "中危"
SEVERITY_LOW = "低危"
SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}

TAINT_LABEL = {RAW: "原始用户输入未净化", WEAK: "仅做弱净化/扩展名校验，可绕过", CLEAN: "已净化"}


def suggestion_for(sink_display: str, weak: bool) -> str:
    """生成可操作的修复建议（含具体函数名/校验逻辑）。"""
    base = (
        "1) 使用 os.path.basename(filename)（或 pathlib.PurePath(filename).name、"
        "werkzeug.utils.secure_filename）剥离任何目录分量，只保留纯文件名；"
        "2) 用 os.path.join(upload_dir, safe_name) 拼接后，再调用 "
        "os.path.realpath()/Path.resolve() 解析为绝对路径；"
        "3) 使用 os.path.commonpath([resolved, upload_root]) == upload_root "
        "（Python 3.9+ 可用 Path.is_relative_to(upload_root)）校验解析后的路径仍在上传根目录内，"
        "校验失败立即拒绝并记录日志；4) 文件名建议改用 uuid.uuid4().hex 服务端重命名。"
    )
    if weak:
        return (
            "当前仅使用黑名单替换/扩展名校验，可被 `....//`、嵌套 `..` 或后缀拼接绕过。" + base
        )
    return f"用户输入被直接拼入 {sink_display} 的写入路径。" + base
