# -*- coding: utf-8 -*-
"""
yaml_scanner.py —— 本地 YAML 部署配置安全扫描器（纯标准库，无第三方依赖）

检测三类缺陷：
  1. hardcoded-credential (high)   键名含 password/secret/token，值为非空、非 null、
                                   非环境变量引用(${VAR})的字符串
  2. dangerous-command    (high)   字符串值含 rm -rf 或 curl 管道 bash 模式
  3. debug-port-exposure  (medium) 键名为 debug_port/debug，值为 1..65535 的整数或数字字符串

用法：
  python yaml_scanner.py [scan] <目录> [--min-severity high|medium|low] [--output report.json]
  python yaml_scanner.py --compare <修复前目录> <修复后目录> [--min-severity ...] [--output ...]
  python yaml_scanner.py --help

其中 scan 为可选关键字，仅用于语义清晰，省略时效果相同。

退出码：
  0  正常（包括扫描到漏洞、单个文件解析失败、目录无 YAML 文件）
  2  参数非法 / 路径不是目录 / 报告文件无法写入
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

YAML_EXTS = (".yaml", ".yml")
SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}
ENV_VAR_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?}$")
# rm -rf 各变体：rm -rf /、rm -fr /tmp/x、rm  -rf、rm --recursive --force
RM_RF_RE = re.compile(
    r"\brm\s+(?:-[A-Za-z]*[rf][A-Za-z]*\s+|"
    r"--(?:recursive|force)\s+[^|;&\n]*--(?:recursive|force)\s+)"
)
# curl/wget 下载后管道给 bash/sh/zsh
CURL_PIPE_BASH_RE = re.compile(
    r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh)\b"
)
CREDENTIAL_KEY_TOKENS = ("password", "secret", "token")
DEBUG_KEY_NAMES = ("debug_port", "debug")

RULE_CREDENTIAL = "hardcoded-credential"
RULE_COMMAND = "dangerous-command"
RULE_DEBUG_PORT = "debug-port-exposure"


class YamlError(Exception):
    """YAML 语法错误，消息中带行号信息。"""


# ---------------------------------------------------------------------------
# 极简 YAML 子集解析器（基于缩进，每个节点保留起始行号，自动忽略注释）
# 支持：map / sequence（块式与流式）、单双引号字符串、字面量|与折叠>块标量、
#       多文档 ---、锚点/别名（最小支持）、null/bool/int/float 标量类型推断
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """解析树节点：value 为 dict[str, Node] / list[Node] / 标量，line 为起始行号。"""
    value: object
    line: int


@dataclass
class _Line:
    indent: int          # 前导空格数
    content: str         # 去掉缩进与注释后的内容
    raw: str             # 原始物理行（块标量保留注释文本用）
    number: int          # 从 1 开始的物理行号


def _strip_comment(text: str) -> str:
    """去掉行内 YAML 注释（# 前必须是行首或空白），保留引号内的 #。"""
    out = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            if i == 0 or text[i - 1] in " \t":
                break
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _find_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue
        body = _strip_comment(raw)
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        if "\t" in body[:indent]:
            raise YamlError(f"第 {number} 行: 缩进不允许使用 Tab 字符")
        lines.append(_Line(indent=indent, content=body.strip(), raw=raw, number=number))
    return lines


class _Parser:
    def __init__(self, lines: list[_Line]):
        self.lines = lines
        self.pos = 0
        self.anchors: dict[str, Node] = {}

    def parse(self) -> Node | None:
        if not self.lines:
            return None
        node = self.parse_node(0)
        if self.pos != len(self.lines):
            ln = self.lines[self.pos].number
            raise YamlError(f"第 {ln} 行: 缩进异常，存在无法归属的内容")
        return node

    def parse_node(self, indent: int) -> Node:
        if self.pos >= len(self.lines):
            raise YamlError("文档意外结束")
        line = self.lines[self.pos]
        if line.indent < indent:
            raise YamlError(f"第 {line.number} 行: 缩进不足")
        if line.content == "-" or line.content.startswith("- "):
            return self.parse_seq(line.indent)
        return self.parse_map(line.indent)

    # ---- map -------------------------------------------------------------
    def parse_map(self, indent: int) -> Node:
        result: dict[str, Node] = {}
        first_line = self.lines[self.pos].number
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError(f"第 {line.number} 行: 缩进异常（期望 {indent} 个空格）")
            if line.content == "-" or line.content.startswith("- "):
                raise YamlError(f"第 {line.number} 行: 此处不应出现序列项")
            key_str, has_colon, rest = self._split_kv(line.content, line.number)
            if not has_colon:
                raise YamlError(f"第 {line.number} 行: 映射项缺少 ':'（{line.content}）")
            key = str(self.parse_scalar(key_str.strip(), line.number))
            rest = rest.strip()
            self.pos += 1
            if rest == "":
                # 嵌套结构或 null
                if self.pos < len(self.lines) and self.lines[self.pos].indent > indent:
                    value = self.parse_node(self.lines[self.pos].indent)
                else:
                    value = Node(None, line.number)
            elif rest[0] in "[{":
                value = self.parse_flow(rest, line.number)
            elif rest[0] in "|>":
                value = Node(self.parse_block_scalar(rest, indent), line.number)
            else:
                value = Node(self.parse_scalar(rest, line.number), line.number)
            if rest.startswith("&"):
                name = rest.split(None, 1)[0][1:]
                self.anchors[name] = value
            result[key] = value
        return Node(result, first_line)

    # ---- sequence --------------------------------------------------------
    def parse_seq(self, indent: int) -> Node:
        result: list[Node] = []
        first_line = self.lines[self.pos].number
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent != indent:
                if line.indent < indent:
                    break
                raise YamlError(f"第 {line.number} 行: 序列项缩进不一致（期望 {indent} 个空格）")
            if not (line.content == "-" or line.content.startswith("- ")):
                break
            after = "" if line.content == "-" else line.content[2:].strip()
            if after == "":
                self.pos += 1
                if self.pos < len(self.lines) and self.lines[self.pos].indent > indent:
                    result.append(self.parse_node(self.lines[self.pos].indent))
                else:
                    result.append(Node(None, line.number))
                continue
            if after[0] in "[{":
                result.append(self.parse_flow(after, line.number))
                continue
            if after[0] in "|>":
                value = Node(self.parse_block_scalar(after, indent), line.number)
                result.append(value)
                continue
            if self._is_map_entry(after):
                # “- key: value” 序列内联 map：视作缩进 +2 的 map 行，行号保持不变
                child_indent = indent + 2
                self.lines[self.pos] = _Line(child_indent, after, line.raw, line.number)
                result.append(self.parse_map(child_indent))
            else:
                result.append(Node(self.parse_scalar(after, line.number), line.number))
                self.pos += 1
        return Node(result, first_line)

    # ---- 流式集合 [..] / {..}（可跨物理行，结束后 pos 指向末行的下一行） --
    def parse_flow(self, text: str, line_no: int) -> Node:
        start = self.pos
        depth = 0
        quote = None
        last = start
        buf = text
        idx = start
        while True:
            for ch in buf:
                if quote:
                    if ch == quote:
                        quote = None
                    continue
                if ch in ("'", '"'):
                    quote = ch
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
            if depth == 0:
                last = idx
                break
            idx += 1
            if idx >= len(self.lines):
                raise YamlError(f"第 {line_no} 行: 流式集合缺少闭合括号")
            buf = self.lines[idx].content
        if quote:
            raise YamlError(f"第 {line_no} 行: 流式集合中引号未闭合")
        if idx > start:
            text = " ".join(self.lines[k].content for k in range(start, idx + 1))
        self.pos = last + 1
        return Node(self._parse_flow_text(text, line_no), line_no)

    def _parse_flow_text(self, text: str, line_no: int):
        opener = text[0]
        closer = text[-1]
        if opener == "[" and closer != "]":
            raise YamlError(f"第 {line_no} 行: 流式序列缺少 ']'")
        if opener == "{" and closer != "}":
            raise YamlError(f"第 {line_no} 行: 流式映射缺少 '}}'")
        body = text[1:-1].strip()
        if not body:
            return [] if opener == "[" else {}
        tokens = self._split_flow_tokens(body, line_no)
        if opener == "[":
            return [self._flow_value(tok.strip(), line_no) for tok in tokens]
        result: dict[str, Node] = {}
        for tok in tokens:
            key_str, has_colon, rest = self._split_kv(tok, line_no)
            if not has_colon:
                raise YamlError(f"第 {line_no} 行: 流式映射项 '{tok}' 缺少 ':'")
            result[str(self.parse_scalar(key_str.strip(), line_no))] = (
                self._flow_value(rest.strip(), line_no))
        return result

    def _flow_value(self, text: str, line_no: int) -> Node:
        """流式集合中的值：嵌套集合递归解析，标量统一包装为 Node。"""
        if text and text[0] in "[{":
            return Node(self._parse_flow_text(text, line_no), line_no)
        return Node(self.parse_scalar(text, line_no), line_no)

    @staticmethod
    def _split_flow_tokens(body: str, line_no: int) -> list[str]:
        tokens: list[str] = []
        depth = 0
        quote = None
        start = 0
        for i, ch in enumerate(body):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                tokens.append(body[start:i])
                start = i + 1
        if quote:
            raise YamlError(f"第 {line_no} 行: 引号未闭合")
        tokens.append(body[start:])
        return tokens

    # ---- 块标量 | 与 > ----------------------------------------------------
    def parse_block_scalar(self, header: str, parent_indent: int) -> str:
        literal = header[0] == "|"
        chomp = next((ch for ch in header[1:] if ch in "+-"), None)
        block_indent: int | None = None
        pieces: list[str] = []
        scan = self.pos + 1
        while scan < len(self.lines):
            raw = self.lines[scan].raw
            if raw.strip() == "":
                pieces.append("")
                scan += 1
                continue
            cur_indent = len(raw) - len(raw.lstrip(" "))
            if cur_indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = cur_indent
            pieces.append(raw[block_indent:] if len(raw) >= block_indent else raw.lstrip(" "))
            scan += 1
        # 直接定位到块外第一行，由上层继续解析（调用方不再推进 pos）
        self.pos = scan
        if literal:
            joined = "\n".join(pieces)
        else:
            joined = self._fold_scalar(pieces)
        if chomp == "-":
            return joined.rstrip("\n")
        if chomp == "+":
            return joined.rstrip("\n") + "\n\n"
        return joined.rstrip("\n") + "\n"

    @staticmethod
    def _fold_scalar(pieces: list[str]) -> str:
        out: list[str] = []
        for piece in pieces:
            if piece == "":
                out.append("\n")
            else:
                out.append(piece + " ")
        return "".join(out).rstrip(" ")

    # ---- 标量 ------------------------------------------------------------
    def parse_scalar(self, text: str, line_no: int):
        text = text.strip()
        if text == "":
            return None
        if text.startswith("*"):
            name = text[1:].strip()
            if name not in self.anchors:
                raise YamlError(f"第 {line_no} 行: 未知的锚点别名 '{name}'")
            return self.anchors[name].value
        if text[0] == "'":
            end = self._quoted_end(text, "'", line_no)
            if end != len(text) - 1:
                raise YamlError(f"第 {line_no} 行: 单引号标量后存在多余内容")
            return text[1:-1].replace("''", "'")
        if text[0] == '"':
            end = self._quoted_end(text, '"', line_no)
            if end != len(text) - 1:
                raise YamlError(f"第 {line_no} 行: 双引号标量后存在多余内容")
            return self._unescape_double(text[1:-1], line_no)
        low = text.lower()
        if low in ("null", "~"):
            return None
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if re.fullmatch(r"[-+]?[0-9]+", text):
            try:
                return int(text)
            except ValueError:
                return text
        if re.fullmatch(r"[-+]?[0-9]*\.[0-9]+", text):
            try:
                return float(text)
            except ValueError:
                return text
        return text

    @staticmethod
    def _quoted_end(text: str, quote: str, line_no: int) -> int:
        i = 1
        while i < len(text):
            if quote == "'" and text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                return i
            if quote == '"':
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    return i
            i += 1
        raise YamlError(f"第 {line_no} 行: 引号未闭合")

    @staticmethod
    def _unescape_double(body: str, line_no: int) -> str:
        simple = {
            "n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
            "0": "\0", "b": "\b", "f": "\f", "/": "/",
        }
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            i += 1
            if i >= len(body):
                raise YamlError(f"第 {line_no} 行: 非法转义序列")
            e = body[i]
            if e in simple:
                out.append(simple[e])
                i += 1
            elif e in ("u", "x"):
                width = 4 if e == "u" else 2
                hexd = body[i + 1:i + 1 + width]
                if len(hexd) != width or not re.fullmatch(r"[0-9A-Fa-f]+", hexd):
                    raise YamlError(f"第 {line_no} 行: 非法 unicode 转义")
                out.append(chr(int(hexd, 16)))
                i += 1 + width
            else:
                raise YamlError(f"第 {line_no} 行: 未知转义序列 '\\{e}'")
        return "".join(out)

    # ---- 辅助 ------------------------------------------------------------
    @staticmethod
    def _split_kv(content: str, line_no: int):
        """返回 (key 原文, 是否有冒号, value 原文)，冒号必须在引号外且后接空白/行尾。"""
        quote = None
        for i, ch in enumerate(content):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
                return content[:i], True, content[i + 1:]
        return content, False, ""

    @staticmethod
    def _is_map_entry(text: str) -> bool:
        quote = None
        for i, ch in enumerate(text):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
                return True
        return False


def parse_yaml(text: str) -> Node | None:
    """解析 YAML 文本，返回带行号的 Node 树；语法错误抛 YamlError。"""
    return _Parser(_find_lines(text)).parse()


# ---------------------------------------------------------------------------
# 规则检测（遍历带行号的 Node 树）
# ---------------------------------------------------------------------------

def _is_env_reference(value: str) -> bool:
    return bool(ENV_VAR_RE.match(value.strip()))


def _is_valid_port(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1 <= value <= 65535
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        try:
            return 1 <= int(value.strip()) <= 65535
        except ValueError:
            return False
    return False


def _make_finding(line: int, key_path: str, rule: str, severity: str,
                  pattern: str | None = None) -> dict:
    return {
        "line": line,
        "key_path": key_path,
        "rule": rule,
        "severity": severity,
        "pattern": pattern,
    }


def detect(node: Node | None, key_path: str) -> list[dict]:
    """递归遍历 Node 树收集漏洞。"""
    findings: list[dict] = []
    if node is None:
        return findings
    value = node.value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{key_path}.{key}" if key_path else str(key)
            findings.extend(_check_scalar_rules(key, child, child_path))
            if isinstance(child.value, (dict, list)):
                findings.extend(detect(child, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{key_path}[{index}]"
            if isinstance(item.value, str):
                findings.extend(_check_command(item.value, child_path, item.line))
            elif isinstance(item.value, (dict, list)):
                findings.extend(detect(item, child_path))
    return findings


def _check_scalar_rules(key: str, value_node: Node, key_path: str) -> list[dict]:
    findings: list[dict] = []
    value = value_node.value
    line = value_node.line
    key_lower = str(key).lower()

    # 规则 1：硬编码凭据
    if any(token in key_lower for token in CREDENTIAL_KEY_TOKENS):
        if isinstance(value, str) and value.strip() != "" and not _is_env_reference(value):
            findings.append(_make_finding(line, key_path, RULE_CREDENTIAL, "high"))

    # 规则 3：调试端口
    if key_lower in DEBUG_KEY_NAMES and _is_valid_port(value):
        findings.append(_make_finding(line, key_path, RULE_DEBUG_PORT, "medium"))

    # 规则 2：危险命令（map 的字符串值）
    if isinstance(value, str):
        findings.extend(_check_command(value, key_path, line))
    return findings


def _check_command(value: str, key_path: str, line: int) -> list[dict]:
    findings: list[dict] = []
    if RM_RF_RE.search(value):
        findings.append(_make_finding(line, key_path, RULE_COMMAND, "high", "rm -rf"))
    if CURL_PIPE_BASH_RE.search(value):
        findings.append(_make_finding(line, key_path, RULE_COMMAND, "high", "curl-pipe-bash"))
    return findings


# ---------------------------------------------------------------------------
# 扫描编排
# ---------------------------------------------------------------------------

def find_yaml_files(directory: str) -> list[str]:
    """递归收集 .yaml/.yml，返回相对路径（正斜杠），按字典序排列。"""
    collected: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(YAML_EXTS):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, directory).replace(os.sep, "/")
                collected.append(rel)
    collected.sort()
    return collected


def _empty_report(directory: str) -> dict:
    return {
        "scan_root": directory.replace(os.sep, "/"),
        "message": "no files found",
        "summary": {"total_files": 0, "files_with_vulnerabilities": 0,
                    "total_vulnerabilities": 0,
                    "by_severity": {"high": 0, "medium": 0, "low": 0}},
        "files": [],
    }


def scan_file(directory: str, rel_path: str) -> dict:
    full = os.path.join(directory, rel_path.replace("/", os.sep))
    try:
        with open(full, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {
            "path": rel_path,
            "status": "error",
            "reason": f"文件不可读: {exc.__class__.__name__}: {exc}",
            "vulnerabilities": [],
        }
    try:
        node = parse_yaml(text)
    except YamlError as exc:
        return {
            "path": rel_path,
            "status": "error",
            "reason": str(exc),
            "vulnerabilities": [],
        }
    except UnicodeDecodeError as exc:
        return {
            "path": rel_path,
            "status": "error",
            "reason": f"编码错误: {exc}",
            "vulnerabilities": [],
        }
    findings = detect(node, "")
    findings.sort(key=lambda f: (f["line"], f["rule"], f["key_path"], f["pattern"] or ""))
    return {
        "path": rel_path,
        "status": "ok",
        "vulnerabilities": findings,
    }


def _severity_passed(finding: dict, min_severity: str | None) -> bool:
    if min_severity is None:
        return True
    return SEVERITY_ORDER[finding["severity"]] >= SEVERITY_ORDER[min_severity]


def scan_directory(directory: str, min_severity: str | None = None) -> dict:
    files = find_yaml_files(directory)
    if not files:
        return _empty_report(directory)
    report_files: list[dict] = []
    for rel in files:
        entry = scan_file(directory, rel)
        entry["vulnerabilities"] = [
            f for f in entry["vulnerabilities"] if _severity_passed(f, min_severity)
        ]
        report_files.append(entry)
    by_sev = {"high": 0, "medium": 0, "low": 0}
    dirty = 0
    total = 0
    for entry in report_files:
        if entry["vulnerabilities"]:
            dirty += 1
        for f in entry["vulnerabilities"]:
            by_sev[f["severity"]] += 1
            total += 1
    report_files.sort(key=lambda e: e["path"])
    return {
        "scan_root": directory.replace(os.sep, "/"),
        "message": "scan completed",
        "summary": {
            "total_files": len(report_files),
            "files_with_vulnerabilities": dirty,
            "total_vulnerabilities": total,
            "by_severity": by_sev,
        },
        "files": report_files,
    }


# ---------------------------------------------------------------------------
# 前后比对
# ---------------------------------------------------------------------------

def _finding_key(finding: dict) -> tuple:
    """按规则名 + 点分键路径对齐。"""
    return finding["rule"], finding["key_path"]


def _align(before_findings: list[dict], after_findings: list[dict]):
    before_counter = Counter(_finding_key(f) for f in before_findings)
    after_counter = Counter(_finding_key(f) for f in after_findings)
    remaining, resolved, introduced = [], [], []
    for finding in before_findings:
        key = _finding_key(finding)
        if after_counter.get(key, 0) > 0:
            remaining.append(finding)
            after_counter[key] -= 1
        else:
            resolved.append(finding)
    for finding in after_findings:
        key = _finding_key(finding)
        if before_counter.get(key, 0) > 0:
            before_counter[key] -= 1
        else:
            introduced.append(finding)
    sort_key = lambda f: (f["line"], f["rule"], f["key_path"], f["pattern"] or "")
    return sorted(remaining, key=sort_key), sorted(resolved, key=sort_key), \
        sorted(introduced, key=sort_key)


def compare_directories(before_dir: str, after_dir: str,
                        min_severity: str | None = None) -> dict:
    before_report = scan_directory(before_dir, min_severity)
    after_report = scan_directory(after_dir, min_severity)
    before_map = {e["path"]: e for e in before_report["files"]}
    after_map = {e["path"]: e for e in after_report["files"]}

    introduced: list[dict] = []
    resolved: list[dict] = []
    remaining: list[dict] = []
    uncomparable: list[dict] = []

    for path in sorted(set(before_map) & set(after_map)):
        b, a = before_map[path], after_map[path]
        if b["status"] != "ok" or a["status"] != "ok":
            uncomparable.append({
                "path": path,
                "side": "both",
                "reason": "before: {0}; after: {1}".format(
                    b.get("reason", "ok") if b["status"] != "ok" else "ok",
                    a.get("reason", "ok") if a["status"] != "ok" else "ok"),
            })
            continue
        rem, res, intro = _align(b["vulnerabilities"], a["vulnerabilities"])
        remaining.extend({"file": path, **f} for f in rem)
        resolved.extend({"file": path, **f} for f in res)
        introduced.extend({"file": path, **f} for f in intro)

    for path in sorted(set(before_map) - set(after_map)):
        uncomparable.append({
            "path": path, "side": "before",
            "reason": "文件仅存在于修复前快照，无法比对",
        })
    for path in sorted(set(after_map) - set(before_map)):
        uncomparable.append({
            "path": path, "side": "after",
            "reason": "文件仅存在于修复后快照，无法比对",
        })
    uncomparable.sort(key=lambda item: (item["side"], item["path"]))

    list_key = lambda f: (f["file"], f["line"], f["rule"], f["key_path"], f["pattern"] or "")
    introduced.sort(key=list_key)
    resolved.sort(key=list_key)
    remaining.sort(key=list_key)

    return {
        "mode": "compare",
        "before_root": before_dir.replace(os.sep, "/"),
        "after_root": after_dir.replace(os.sep, "/"),
        "message": "compare completed",
        "summary": {
            "introduced": len(introduced),
            "resolved": len(resolved),
            "remaining": len(remaining),
            "uncomparable_files": len(uncomparable),
        },
        "introduced": introduced,
        "resolved": resolved,
        "remaining": remaining,
        "uncomparable": uncomparable,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yaml_scanner.py",
        description="本地 YAML 部署配置安全扫描器：检测硬编码凭据、危险命令、调试端口暴露，"
                    "支持修复前后快照比对。纯标准库实现，无需安装第三方依赖。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python yaml_scanner.py scan ./snapshots/before\n"
               "  python yaml_scanner.py ./snapshots/before\n"
               "  python yaml_scanner.py scan ./cfg --min-severity medium --output report.json\n"
               "  python yaml_scanner.py --compare ./before ./after --output diff.json\n",
    )
    parser.add_argument("directory", nargs="?",
                        help="待扫描目录（递归扫描其中所有 .yaml/.yml 文件）；"
                             "前面可加可选关键字 scan")
    parser.add_argument("--min-severity", default=None,
                        help="最低风险级别过滤，取值 high / medium / low，不区分大小写；"
                             "例如 medium 表示仅输出 high 与 medium 漏洞")
    parser.add_argument("--output", default=None,
                        help="UTF-8 编码的 JSON 报告输出文件路径；未指定时写入标准输出")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE_DIR", "AFTER_DIR"),
                        help="比对修复前与修复后两个目录，输出新增 / 已消除 / 仍存在 / "
                             "不可比对四类清单")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # 兼容可选的 scan 关键字：python yaml_scanner.py scan <目录>
    if raw_args and raw_args[0] == "scan":
        raw_args = raw_args[1:]
    args = parser.parse_args(raw_args)

    min_severity = None
    if args.min_severity is not None:
        min_severity = args.min_severity.lower()
        if min_severity not in SEVERITY_ORDER:
            sys.stderr.write(
                f"错误: --min-severity 非法值 '{args.min_severity}'，"
                f"可选值为 high / medium / low\n")
            return 2

    try:
        if args.compare:
            before_dir, after_dir = args.compare
            for path in (before_dir, after_dir):
                if not os.path.isdir(path):
                    sys.stderr.write(f"错误: 路径不存在或不是目录: {path}\n")
                    return 2
            report = compare_directories(
                os.path.abspath(before_dir), os.path.abspath(after_dir), min_severity)
        else:
            if not args.directory:
                parser.error("缺少待扫描目录参数（或使用 --compare 进行前后比对）")
            target = args.directory
            if not os.path.isdir(target):
                # 路径不存在：空结果 + no files found，退出码 0（验收第 8 条）
                report = _empty_report(target)
            else:
                report = scan_directory(os.path.abspath(target), min_severity)
    except OSError as exc:
        sys.stderr.write(f"错误: 扫描过程发生 IO 异常: {exc}\n")
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
        except OSError as exc:
            sys.stderr.write(f"错误: 无法写入报告文件 '{args.output}': {exc}\n")
            return 2
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
