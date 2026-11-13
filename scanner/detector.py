# -*- coding: utf-8 -*-
"""基于 AST 的路径穿越静态扫描器（函数内污点分析）。

分析思路：
1. 逐文件 ast.parse；语法错误/解码错误记入 errors 并跳过，不中断整体扫描。
2. 在每个函数定义内做轻量污点传播：
   - 污点源：request.FILES/form/args、input()、os.getenv() 等用户可控输入；
   - 传播：字符串拼接(+/%)、f-string、os.path.join、Path(...)、赋值、函数实参；
   - 强净化：basename / PurePath.name / secure_filename / uuid 重命名 -> CLEAN；
   - 弱净化：str.replace 黑名单、strip、纯扩展名校验 -> WEAK（可绕过）；
3. 汇聚点(sink)：open/write_text/write_bytes/copy*/save 等写入类，
   以及 read_text/read_bytes/remove/unlink/rmtree 等读删类；
4. 函数内存在 commonpath/resolve+startswith/is_relative_to 边界守卫时抑制告警。
"""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field

from . import rules as R


@dataclass
class Finding:
    file: str           # 相对扫描根目录的路径
    line: int
    severity: str       # 高危 / 中危 / 低危
    function: str
    sink: str
    taint_level: int
    taint_source: str
    code: str
    suggestion: str
    fingerprint: str
    id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "function": self.function,
            "sink": self.sink,
            "taint_source": self.taint_source,
            "taint_level": R.TAINT_LABEL[self.taint_level],
            "code": self.code.strip(),
            "suggestion": self.suggestion,
            "fingerprint": self.fingerprint,
        }


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def dotted_name(node: ast.AST) -> str:
    """把 Attribute/Subscript/Name 链还原成点号名称（忽略下标内容）。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def chain_contains_request(node: ast.AST) -> bool:
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        if isinstance(cur, ast.Attribute) and cur.attr in R.REQUEST_ATTRS:
            return True
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id in {"request", "req"}:
        return True
    return False


def _literal_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# ---------------------------------------------------------------------------
# 函数级分析器
# ---------------------------------------------------------------------------
class _FuncAnalyzer:
    def __init__(self, rel_path: str, func: ast.FunctionDef | ast.AsyncFunctionDef,
                 aliases: dict[str, str]):
        self.rel_path = rel_path
        self.func = func
        self.aliases = aliases
        self.env: dict[str, int] = {}                 # 局部变量名 -> 污点等级
        self.source_desc: dict[str, str] = {}         # 变量名 -> 污点源描述
        self.raw_findings: list[dict] = []
        self.guarded = self._detect_guard()
        # 函数内是否出现“真实外部污点源”（request/input/env/CGI）。
        # 仅当存在外部源时，文件名形参参与的 sink 才告警，避免工具自身内部路径误报。
        self.has_external_source = self._detect_external_source()
        # 函数内是否使用弱净化（replace 黑名单等），无外部源但有弱净化仍需提示
        self.has_weak_sanitizer = self._detect_weak_sanitizer()
        # 上传服务入口函数（命名约定）：其文件名形参天然视为外部可控
        self.is_upload_entry = func.name in {
            "save_upload", "upload_file", "handle_upload", "store_file",
            "receive_upload", "upload", "save_file",
        }

    # -- 守卫检测：commonpath / resolve+startswith / is_relative_to ----------
    def _detect_guard(self) -> bool:
        """仅当“路径规范化调用”与“路径边界检查”同时出现才判定为守卫。

        普通相等比较（如扩展名 endswith 判断）不算边界守卫。
        """
        guard_call = False
        guard_check = False
        for n in ast.walk(self.func):
            if isinstance(n, ast.Call):
                name = dotted_name(n.func)
                tail = name.rsplit(".", 1)[-1]
                if tail in {"resolve", "realpath", "commonpath"} \
                        or name in R.CONTAINMENT_GUARDS:
                    guard_call = True
                if isinstance(n.func, ast.Attribute) and n.func.attr in R.GUARD_CHECK_ATTRS:
                    # startswith/is_relative_to/relative_to 的接收者需是路径变量
                    guard_check = True
        return guard_call and guard_check

    def _detect_external_source(self) -> bool:
        """函数体是否引用 request/input/getenv/FieldStorage 等外部输入。"""
        for n in ast.walk(self.func):
            if isinstance(n, ast.Call) and self._source_desc_for(n) is not None:
                return True
            if isinstance(n, (ast.Attribute, ast.Subscript)) and chain_contains_request(n):
                return True
        return False

    def _detect_weak_sanitizer(self) -> bool:
        for n in ast.walk(self.func):
            if isinstance(n, ast.Call):
                name = dotted_name(n.func)
                tail = name.rsplit(".", 1)[-1]
                if name in R.WEAK_SANITIZERS or tail in {"replace", "strip", "lstrip", "rstrip"}:
                    return True
        return False

    # -- 污点源判定 ----------------------------------------------------------
    def _source_desc_for(self, node: ast.Call) -> str | None:
        name = dotted_name(node.func)
        alias_name = self._apply_alias(name)
        for key, desc in R.TAINT_SOURCES.items():
            if name == key or alias_name == key:
                return desc
        # request.xxx 的任意方法/下标链，如 request.form.get('f')、request.FILES['x']
        if chain_contains_request(node):
            for attr, desc in R.REQUEST_ATTRS.items():
                if f".{attr}" in f".{name}":
                    return desc
            return "HTTP 请求参数"
        # 任意变量.form/.files/.GET... 的方法调用（测试桩 request 对象等）
        if isinstance(node.func, ast.Attribute):
            base_chain = dotted_name(node.func.value)
            for attr, desc in R.REQUEST_ATTRS.items():
                if base_chain.endswith(f".{attr}") or base_chain == attr:
                    return desc
        # input() / os.getenv() 短名
        tail = name.rsplit(".", 1)[-1]
        if tail == "input":
            return R.TAINT_SOURCES["input"]
        if tail == "getenv":
            return R.TAINT_SOURCES["os.getenv"]
        return None

    def _apply_alias(self, name: str) -> str:
        head = name.split(".", 1)[0]
        if head in self.aliases:
            return self.aliases[head] + name[len(head):]
        return name

    # -- 表达式污点评估 ------------------------------------------------------
    def eval(self, node: ast.AST) -> int:
        if node is None:
            return R.CLEAN
        if isinstance(node, ast.Constant):
            return R.CLEAN
        if isinstance(node, ast.Name):
            return self.env.get(node.id, R.CLEAN)
        if isinstance(node, ast.Subscript):
            # request.FILES['f'] / request.form['x']
            if chain_contains_request(node):
                return R.RAW
            return self.eval(node.value)
        if isinstance(node, ast.Attribute):
            name = dotted_name(node)
            # file.filename / file.name（上传文件对象取文件名）
            if node.attr in R.FILENAME_ATTRS and self.eval(node.value) == R.RAW:
                return R.RAW
            if chain_contains_request(node):
                return R.RAW
            # Path(x).name / PurePath(x).name —— 强净化（仅对 Path 构造结果生效）
            if node.attr == "name" and isinstance(node.value, ast.Call):
                callee = dotted_name(node.value.func)
                if callee.endswith("Path") or callee.endswith("PurePath"):
                    return R.CLEAN
            return self.eval(node.value)
        if isinstance(node, ast.BinOp):
            # +/% 字符串拼接、/ 为 pathlib.Path 除法拼接，均传播污点
            if isinstance(node.op, (ast.Add, ast.Mod, ast.Div)):
                return max(self.eval(node.left), self.eval(node.right))
            return R.CLEAN
        if isinstance(node, ast.JoinedStr):  # f-string
            vals = [self.eval(v) for v in node.values if isinstance(v, ast.FormattedValue)]
            return max(vals, default=R.CLEAN)
        if isinstance(node, ast.FormattedValue):
            return self.eval(node.value)
        if isinstance(node, ast.Call):
            return self._eval_call(node)
        if isinstance(node, ast.IfExp):
            return max(self.eval(node.body), self.eval(node.test), self.eval(node.orelse))
        if isinstance(node, ast.BoolOp):
            return max(self.eval(v) for v in node.values)
        if isinstance(node, ast.UnaryOp):
            return self.eval(node.operand)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return max((self.eval(e) for e in node.elts), default=R.CLEAN)
        if isinstance(node, ast.Dict):
            return max((max(self.eval(k), self.eval(v)) for k, v in zip(node.keys, node.values)
                        if k is not None and v is not None), default=R.CLEAN)
        if isinstance(node, ast.Starred):
            return self.eval(node.value)
        return R.CLEAN

    def _eval_call(self, node: ast.Call) -> int:
        name = dotted_name(node.func)
        tail = name.rsplit(".", 1)[-1]
        args_taint = max([self.eval(a) for a in node.args] +
                         [self.eval(k.value) for k in node.keywords], default=R.CLEAN)

        # 1) 污点源
        desc = self._source_desc_for(node)
        if desc is not None:
            return R.RAW

        # 2) FileStorage 保存方法 —— 写入 sink
        if isinstance(node.func, ast.Attribute) and node.func.attr in R.SAVE_METHODS:
            self._record_sink(node, "save", args_taint, write=True, taint_obj=node.func.value)
            return args_taint

        # 3) 强净化（仅匹配完整规则键，避免别名归一化误伤 str.replace 等）
        if name in R.STRONG_SANITIZERS:
            return R.CLEAN
        if name.startswith(("os.path.", "posixpath.", "ntpath.")) \
                and self._apply_alias(name) in R.STRONG_SANITIZERS:
            return R.CLEAN
        if tail in {"hex"} and "uuid" in name:
            return R.CLEAN
        if tail in {"uuid4", "uuid1"}:
            return R.CLEAN

        # 4) 弱净化（保留原污点，降为 WEAK，便于报告“可绕过”）
        if name in R.WEAK_SANITIZERS or tail in {"replace", "strip", "lstrip", "rstrip"}:
            return max(R.WEAK, args_taint)

        # 5) 路径拼接/构造 —— 传播实参污点
        if tail == "join" and ("os.path" in name or "posixpath" in name or "ntpath" in name):
            return args_taint
        if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
            return args_taint
        if tail == "Path" or name.endswith("pathlib.Path"):
            return args_taint
        if tail in {"resolve", "realpath", "abspath", "normpath"}:
            return args_taint  # 规范化不等于净化，守卫判定另做
        if tail in R.CONTAINMENT_GUARDS:
            return R.CLEAN  # commonpath 等比较函数本身产出安全布尔/相对路径

        # 5.1) Path 方法型 sink：<expr>.write_bytes/read_bytes/unlink(...)
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in R.PATH_WRITE_METHODS:
                path_taint = self.eval(node.func.value)
                self._record_sink(node, R.PATH_WRITE_METHODS[attr],
                                  path_taint, write=True)
                return path_taint
            if attr in R.PATH_READ_METHODS:
                path_taint = self.eval(node.func.value)
                self._record_sink(node, R.PATH_READ_METHODS[attr],
                                  path_taint, write=False)
                return path_taint
            if attr == "unlink":
                path_taint = self.eval(node.func.value)
                self._record_sink(node, "Path.unlink", path_taint, write=False)
                return path_taint

        # 6) 危险汇聚点
        if self._is_write_sink(name):
            resolved = self._resolve_sink_name(name)
            path_taint = self._sink_path_taint(node, tail, write=True)
            self._record_sink(node, R.WRITE_SINKS.get(resolved, tail),
                              path_taint, write=True)
            return path_taint
        resolved = self._resolve_sink_name(name)
        if resolved in R.READ_SINKS:
            path_taint = self._sink_path_taint(node, tail, write=False)
            self._record_sink(node, R.READ_SINKS[resolved], path_taint, write=False)
            return path_taint
        if resolved in R.DELETE_SINKS:
            path_taint = self._sink_path_taint(node, tail, write=False)
            self._record_sink(node, R.DELETE_SINKS[resolved], path_taint, write=False)
            return path_taint

        # 7) 未知函数：保守传播实参污点
        return args_taint

    def _sink_path_taint(self, node: ast.Call, tail: str, write: bool) -> int:
        # open(file, mode)：路径参数为第一个位置参数
        if tail in {"open"}:
            return self.eval(node.args[0]) if node.args else R.CLEAN
        # shutil.copy*(src, dst)：目标参数为第二个位置参数
        if tail.startswith("copy"):
            if len(node.args) >= 2:
                return self.eval(node.args[1])
            return R.CLEAN
        # os.remove/unlink/rmtree(path)
        return self.eval(node.args[0]) if node.args else R.CLEAN

    def _resolve_sink_name(self, name: str) -> str:
        """把 open/os.open 等短名归一化到规则表中的完整键。"""
        alias = self._apply_alias(name)
        if name in R.WRITE_SINKS or name in R.READ_SINKS or name in R.DELETE_SINKS:
            return name
        if alias in R.WRITE_SINKS or alias in R.READ_SINKS or alias in R.DELETE_SINKS:
            return alias
        if name in {"open"} or name.endswith(".open"):
            return "builtins.open"
        return name

    def _is_write_sink(self, name: str) -> bool:
        resolved = self._resolve_sink_name(name)
        return resolved in R.WRITE_SINKS

    def _record_sink(self, node: ast.Call, display: str, taint: int,
                     write: bool, taint_obj: ast.AST | None = None) -> None:
        if taint == R.CLEAN:
            return
        # 门控：仅当函数具备外部输入特征时才告警，避免把工具自身基于命令行配置的
        # 内部文件读写误判为上传穿越。外部特征满足任一即可：
        #   a) 引用 request/input/getenv 等真实外部源；
        #   b) 命名为上传服务入口函数（save_upload/upload_file/...）；
        #   c) 使用了弱净化（replace 黑名单等）。
        if not (self.has_external_source or self.is_upload_entry
                or self.has_weak_sanitizer):
            return
        if write:
            # 写入类汇聚点：原始污点高危，弱净化（可绕过）中危
            severity = R.SEVERITY_HIGH if taint == R.RAW else R.SEVERITY_MEDIUM
        else:
            # 读取/删除类汇聚点：整体风险低于写入，最高定为低危
            severity = R.SEVERITY_LOW
        src_desc = "用户可控输入"
        if taint_obj is not None:
            src_desc = "HTTP 上传文件对象"
        self.raw_findings.append({
            "line": node.lineno,
            "severity": severity,
            "sink": display,
            "taint": taint,
            "source": src_desc,
        })

    def _has_extension_only_check(self) -> bool:
        """函数内是否存在 endswith/rsplit/splitext 扩展名判断（提示仅校验扩展名）。"""
        for n in ast.walk(self.func):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in R.EXTENSION_ONLY_HINTS:
                    return True
        return False

    # -- 语句遍历 ------------------------------------------------------------
    def analyze(self) -> list[dict]:
        # 文件名语义的形参视为用户可控污点源（上传服务入口参数）。
        # 注意：不含 path/filepath/target/dest 等“已确定内部路径”语义名，避免内部工具误报。
        tainted_args = {"filename", "name", "fname", "file_name",
                        "key", "object_name", "upload_name"}
        for arg in (self.func.args.posonlyargs + self.func.args.args
                    + self.func.args.kwonlyargs):
            if arg.arg in tainted_args or arg.arg.endswith(("_name", "_file")):
                self.env[arg.arg] = R.RAW
                self.source_desc[arg.arg] = "函数入参（用户可控文件名）"
            else:
                self.env[arg.arg] = R.CLEAN
        for stmt in self.func.body:
            self._walk_stmt(stmt)
        if self.guarded:
            return []  # 边界守卫存在，函数内穿越告警整体抑制
        return self.raw_findings

    def _walk_stmt(self, stmt: ast.stmt) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return  # 嵌套函数独立分析
        if isinstance(stmt, ast.Assign):
            taint = self.eval(stmt.value)
            desc = None
            if isinstance(stmt.value, ast.Call):
                desc = self._source_desc_for(stmt.value)
            for target in stmt.targets:
                self._assign(target, taint, desc)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            taint = self.eval(stmt.value)
            self._assign(stmt.target, taint, None)
        elif isinstance(stmt, ast.AugAssign):
            rhs = self.eval(stmt.value)
            if isinstance(stmt.target, ast.Name):
                old = self.env.get(stmt.target.id, R.CLEAN)
                self.env[stmt.target.id] = max(old, rhs)
        elif isinstance(stmt, ast.Expr):
            self.eval(stmt.value)
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self.eval(stmt.value)
        elif isinstance(stmt, ast.If):
            self.eval(stmt.test)
            for s in stmt.body + stmt.orelse:
                self._walk_stmt(s)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.eval(stmt.iter)
            for s in stmt.body + stmt.orelse:
                self._walk_stmt(s)
        elif isinstance(stmt, ast.While):
            self.eval(stmt.test)
            for s in stmt.body + stmt.orelse:
                self._walk_stmt(s)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self.eval(item.context_expr)
            for s in stmt.body:
                self._walk_stmt(s)
        elif isinstance(stmt, ast.Try):
            for s in (stmt.body + stmt.orelse + stmt.finalbody):
                self._walk_stmt(s)
            for handler in stmt.handlers:
                for s in handler.body:
                    self._walk_stmt(s)
        elif isinstance(stmt, (ast.Raise, ast.Assert)):
            if getattr(stmt, "msg", None) is not None:
                self.eval(stmt.msg)
        else:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    self._walk_stmt(child)
                else:
                    self.eval(child)

    def _assign(self, target: ast.AST, taint: int, desc: str | None) -> None:
        if isinstance(target, ast.Name):
            self.env[target.id] = taint
            if desc:
                self.source_desc[target.id] = desc
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._assign(elt, taint, desc)
        elif isinstance(target, ast.Starred):
            self._assign(target.value, taint, desc)
        # 下标/属性目标（如 data["x"]=...）不建立变量污点


def name_startswith_os(node: ast.Call) -> bool:  # 保留为工具函数（兼容外部调用）
    name = dotted_name(node.func)
    return name.startswith(("os.", "nt.", "posix."))


# ---------------------------------------------------------------------------
# 文件级与目录级扫描
# ---------------------------------------------------------------------------
def _collect_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                full = f"{mod}.{a.name}" if mod else a.name
                aliases[a.asname or a.name] = full
    return aliases


def scan_file(path: str, rel_path: str) -> tuple[list[Finding], dict | None]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        tree = ast.parse(raw, filename=path)
    except SyntaxError as exc:
        return [], {"file": rel_path, "error": "SyntaxError",
                    "message": f"{exc.msg} (line {exc.lineno})"}
    except UnicodeDecodeError as exc:
        return [], {"file": rel_path, "error": "UnicodeDecodeError", "message": str(exc)}
    except OSError as exc:
        return [], {"file": rel_path, "error": type(exc).__name__, "message": str(exc)}

    aliases = _collect_aliases(tree)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        analyzer = _FuncAnalyzer(rel_path, node, aliases)
        for raw_f in analyzer.analyze():
            line_no = raw_f["line"]
            code = _line_of(raw, line_no)
            fp = _fingerprint(rel_path, node.name, raw_f["sink"], code)
            findings.append(Finding(
                file=rel_path,
                line=line_no,
                severity=raw_f["severity"],
                function=node.name,
                sink=raw_f["sink"],
                taint_level=raw_f["taint"],
                taint_source=raw_f["source"],
                code=code,
                suggestion=R.suggestion_for(raw_f["sink"], weak=raw_f["taint"] == R.WEAK),
                fingerprint=fp,
            ))
    return findings, None


def _line_of(raw: bytes, line_no: int) -> str:
    try:
        return raw.decode("utf-8", errors="replace").splitlines()[line_no - 1]
    except Exception:
        return ""


def _fingerprint(rel_path: str, func: str, sink: str, code: str) -> str:
    base = os.path.basename(rel_path)
    digest = hashlib.sha256(
        f"{base}|{func}|{sink}|{code.strip()}".encode("utf-8")
    ).hexdigest()[:12]
    return f"FP-{digest}"


def scan_directory(root: str) -> ScanResult:
    root = os.path.abspath(root)
    result = ScanResult()
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过缓存与虚拟环境目录
        dirnames[:] = sorted(d for d in dirnames
                             if d not in {"__pycache__", ".venv", "venv", ".git"})
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
            result.scanned_files.append(rel_path)
            findings, error = scan_file(abs_path, rel_path)
            if error is not None:
                result.errors.append(error)
            else:
                result.findings.extend(findings)
    _assign_ids(result.findings)
    return result


def _assign_ids(findings: list[Finding]) -> None:
    ordered = sorted(findings,
                     key=lambda f: (R.SEVERITY_ORDER[f.severity], f.file, f.line, f.sink))
    for idx, f in enumerate(ordered, start=1):
        f.id = f"V{idx:03d}"
