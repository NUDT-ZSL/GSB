"""基于 AST 的三条检查规则（全部只做单文件内分析，不跨文件追踪符号）。

SEC001 外部输入拼接后直接传入路径打开调用（越权读取）
SEC002 递归展开缺少终止条件（资源耗尽）
SEC003 敏感配置值明文写入日志或标准输出（信息泄露）
"""
from __future__ import annotations

import ast
import re

from .model import Finding, RULE_DESCRIPTIONS

# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

#: 视为“敏感配置”的标识符片段（小写子串匹配）
SENSITIVE_PARTS = ("token", "secret", "password", "passwd", "apikey", "api_key")

#: 常见日志对象方法
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}

#: 路径拼接函数（限定为路径语义，避免把普通拼接函数一概视为传播）
PATH_JOIN_FUNCS = ("os.path.join", "posixpath.join", "ntpath.join",
                   "pathlib.Path", "Path")

#: 污点可沿参数传播的内置/标准库函数
TAINT_PROPAGATING_FUNCS = ("str", "bytes", "format", "strip", "lstrip", "rstrip",
                           "replace", "lower", "upper", "encode", "decode")

_SENSITIVE_RE = re.compile("|".join(SENSITIVE_PARTS))


def root_name(node: ast.AST) -> str:
    """返回表达式最左侧的根名称，如 os.path.join -> 'os'；非名称表达式返回 ''。"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return ""


def dotted_name(node: ast.AST) -> str:
    """把 Name/Attribute 链还原为点分名称，无法还原时返回 ''。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def is_sensitive_name(name: str) -> bool:
    """标识符（含下划线风格，如 api_token）是否命名为敏感配置。"""
    return bool(_SENSITIVE_RE.search(name.lower()))


# ---------------------------------------------------------------------------
# 外部输入源（单文件内可直接识别的来源）
# ---------------------------------------------------------------------------

def is_source(node: ast.AST) -> bool:
    """节点是否直接来自外部输入源。

    覆盖：input()、sys.argv[...]、os.environ[...]、HTTP 请求对象取值。
    """
    # input(...) 调用
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input":
        return True
    # sys.argv[...]
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "argv":
            inner = value.value
            if isinstance(inner, ast.Name) and inner.id == "sys":
                return True
        # os.environ[...] / os.getenv(...)
        if isinstance(value, ast.Attribute) and value.attr == "environ":
            inner = value.value
            if isinstance(inner, ast.Name) and inner.id == "os":
                return True
        # request.args[...] / request.form[...] / request.values[...] / request.headers[...]
        if isinstance(value, ast.Attribute) and value.attr in ("args", "form", "values",
                                                                "headers", "cookies", "data",
                                                                "json", "params", "query"):
            return True
    # os.getenv(...) / os.environ.get(...)
    if isinstance(node, ast.Call):
        dotted = dotted_name(node.func)
        if dotted in ("os.getenv", "os.environ.get"):
            return True
        # request.args.get(...) 等
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            base = dotted_name(node.func.value)
            if base.endswith(("args", "form", "values", "headers", "cookies", "params", "query")):
                return True
    return False


# ---------------------------------------------------------------------------
# 单文件污点跟踪：流不敏感，只跟踪“变量 -> 是否被外部输入污染”
# ---------------------------------------------------------------------------

class TaintTracker:
    """在单棵 AST 内做轻量污点分析。

    局限（有意为之，符合“仅单文件分析、不跨文件追踪符号”的约束）：
    - 流不敏感：同一变量在任意赋值中被污染即视为污染；
    - 不建模函数形参/实参的跨过程传播，只识别赋值链与表达式内传播；
    - 不内置净化函数模型（白名单查表可通过“字典下标键不传播”模式自然消除误报）。
    """

    def __init__(self, tree: ast.AST) -> None:
        self.tainted: set[str] = set()
        self._solve(tree)

    def _solve(self, tree: ast.AST) -> None:
        # 不动点迭代，处理变量间反复赋值（a = b; b = c + input() 之类）
        for _ in range(self._max_iterations(tree)):
            before = set(self.tainted)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    if self._expr_tainted(node.value):
                        for target in node.targets:
                            self.tainted.update(self._names(target))
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    if self._expr_tainted(node.value):
                        self.tainted.update(self._names(node.target))
                elif isinstance(node, ast.AugAssign):
                    # x += input()：右侧或 x 本身污染，则 x 污染
                    if self._expr_tainted(node.value) or self._expr_tainted(node.target):
                        self.tainted.update(self._names(node.target))
            if self.tainted == before:
                break

    @staticmethod
    def _max_iterations(tree: ast.AST) -> int:
        return max(10, sum(isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                           for n in ast.walk(tree)) + 2)

    @staticmethod
    def _names(target: ast.AST) -> set[str]:
        """取赋值目标中的普通变量名（元组解包时取全部元素）。"""
        names: set[str] = set()
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                names.update(TaintTracker._names(elt))
        # 属性/下标目标不产生新的局部变量名
        return names

    def is_tainted(self, node: ast.AST) -> bool:
        return self._expr_tainted(node)

    def _expr_tainted(self, node: ast.AST) -> bool:
        # 直接外部输入源
        if is_source(node):
            return True
        # 被污染变量
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        # 二元运算：字符串拼接 / 算术 / 比较等，任一操作数污染即污染
        if isinstance(node, ast.BinOp):
            return self._expr_tainted(node.left) or self._expr_tainted(node.right)
        # f-string / 字符串隐式拼接
        if isinstance(node, ast.JoinedStr):
            return any(self._expr_tainted(v) for v in node.values)
        # 一元运算（如 not、-）
        if isinstance(node, ast.UnaryOp):
            return self._expr_tainted(node.operand)
        # if-else 表达式
        if isinstance(node, ast.IfExp):
            return (self._expr_tainted(node.body) or self._expr_tainted(node.orelse)
                    or self._expr_tainted(node.test))
        # 下标：仅“容器”污染才传播，键不传播——
        # 这样 ALLOWED[user_input] 白名单查表不会被误判为污染。
        if isinstance(node, ast.Subscript):
            return self._expr_tainted(node.value)
        # 属性访问：沿对象传播
        if isinstance(node, ast.Attribute):
            return self._expr_tainted(node.value)
        # 列表/集合/元组字面量
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            return any(self._expr_tainted(e) for e in node.elts)
        # 字典字面量：键和值都检查
        if isinstance(node, ast.Dict):
            return any((k is not None and self._expr_tainted(k)) or self._expr_tainted(v)
                       for k, v in zip(node.keys, node.values))
        # 星号表达式
        if isinstance(node, ast.Starred):
            return self._expr_tainted(node.value)
        # 调用：路径拼接/格式化/编码等沿参数传播；未知调用默认不传播，
        # 避免把 dict.get(污染键) 之类的安全查表误判为污染。
        if isinstance(node, ast.Call):
            dotted = dotted_name(node.func)
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else "")
            propagating = (dotted in PATH_JOIN_FUNCS or name in TAINT_PROPAGATING_FUNCS
                           or (name == "format" and isinstance(node.func, ast.Attribute)))
            if propagating:
                arg_nodes = list(node.args) + [kw.value for kw in node.keywords]
                return any(self._expr_tainted(a) for a in arg_nodes)
            # 方法链对象本身污染（如 tainted_path.read()）也视为污染
            if isinstance(node.func, ast.Attribute) and self._expr_tainted(node.func.value):
                return True
            return False
        # 比较运算
        if isinstance(node, ast.Compare):
            if self._expr_tainted(node.left):
                return True
            return any(self._expr_tainted(c) for c in node.comparators)
        # 布尔运算
        if isinstance(node, ast.BoolOp):
            return any(self._expr_tainted(v) for v in node.values)
        return False


# ---------------------------------------------------------------------------
# SEC001 外部输入拼接后直接打开
# ---------------------------------------------------------------------------

def _is_open_call(call: ast.Call) -> bool:
    """识别内置 open(...) 与 xxx.open(...) 形式的路径打开调用。"""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "open":
        return True
    return False


def check_path_open(tree: ast.AST, rel_path: str) -> list[Finding]:
    """SEC001：污点参数（外部输入或其拼接结果）直接传入路径打开调用。"""
    findings: list[Finding] = []
    tracker = TaintTracker(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_open_call(node) or not node.args:
            continue
        first_arg = node.args[0]
        if tracker.is_tainted(first_arg):
            findings.append(Finding(
                rule_id="SEC001",
                file=rel_path,
                line=node.lineno,
                message=RULE_DESCRIPTIONS["SEC001"],
            ))
    return findings


# ---------------------------------------------------------------------------
# SEC002 递归缺少终止条件
# ---------------------------------------------------------------------------

class _RecursiveCallCollector(ast.NodeVisitor):
    """收集函数体内对指定函数名的递归调用，不进入嵌套函数定义。"""

    def __init__(self, func_name: str) -> None:
        self.func_name = func_name
        self.calls: list[ast.Call] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == self.func_name:
            self.generic_visit(node)  # 顶层目标函数：继续向内
        # 嵌套函数：不进入，避免把内层同名函数的调用算作外层递归

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == self.func_name:
            self.calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == self.func_name:
            # self.expand(...) 形式的递归
            self.calls.append(node)
        self.generic_visit(node)


class _TerminationScanner(ast.NodeVisitor):
    """扫描函数体内是否存在终止结构：if 分支中的 return / raise / break，
    或循环中的 break。不进入嵌套函数定义。

    注意：函数末尾无条件的 return 不算终止条件——无终止递归函数通常也以
    return 收尾，把它计入会导致漏报。
    """

    def __init__(self) -> None:
        self.has_termination = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # 仅顶层函数自身被 walk 时进入；嵌套函数不进入
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_If(self, node: ast.If) -> None:
        for child in ast.walk(node):
            if isinstance(child, (ast.Return, ast.Raise, ast.Break)):
                self.has_termination = True
                return
        self.generic_visit(node)

    def visit_Break(self, node: ast.Break) -> None:
        self.has_termination = True


def check_recursion(tree: ast.AST, rel_path: str) -> list[Finding]:
    """SEC002：存在自递归调用但函数体内未发现任何终止结构。"""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        collector = _RecursiveCallCollector(node.name)
        for stmt in node.body:
            collector.visit(stmt)
        if not collector.calls:
            continue  # 非递归函数
        scanner = _TerminationScanner()
        for stmt in node.body:
            scanner.visit(stmt)
        if not scanner.has_termination:
            findings.append(Finding(
                rule_id="SEC002",
                file=rel_path,
                line=node.lineno,
                message=RULE_DESCRIPTIONS["SEC002"],
            ))
    return findings


# ---------------------------------------------------------------------------
# SEC003 敏感值明文输出
# ---------------------------------------------------------------------------

def _is_output_call(node: ast.Call) -> bool:
    """识别 print(...)、logger.xxx(...)、sys.stdout.write(...) 等输出调用。"""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if isinstance(func, ast.Attribute):
        if func.attr in LOG_METHODS:
            return True
        if func.attr == "write":
            base = dotted_name(func.value)
            if base in ("sys.stdout", "sys.stderr") or base.endswith(("stdout", "stderr")):
                return True
    return False


def _sensitive_descendants(node: ast.AST) -> list[ast.expr]:
    """收集表达式内所有命名为敏感配置的 Name/Attribute 节点。"""
    hits: list[ast.expr] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and is_sensitive_name(child.id):
            hits.append(child)
        elif isinstance(child, ast.Attribute) and is_sensitive_name(child.attr):
            hits.append(child)
    return hits


def check_sensitive_output(tree: ast.AST, rel_path: str) -> list[Finding]:
    """SEC003：输出/日志调用的参数中出现敏感变量，每个敏感节点各报一条（不去重）。"""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_output_call(node):
            continue
        argument_nodes = list(node.args) + [kw.value for kw in node.keywords]
        for arg in argument_nodes:
            for hit in _sensitive_descendants(arg):
                findings.append(Finding(
                    rule_id="SEC003",
                    file=rel_path,
                    line=hit.lineno,
                    message=RULE_DESCRIPTIONS["SEC003"],
                ))
    return findings


# ---------------------------------------------------------------------------
# 规则汇总
# ---------------------------------------------------------------------------

ALL_RULE_CHECKS = (
    check_path_open,
    check_recursion,
    check_sensitive_output,
)
