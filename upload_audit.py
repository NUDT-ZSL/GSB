#!/usr/bin/env python3
"""上传目录落盘核验工具（仅使用 Python 标准库）。

用途
----
读取“上传目录”和“清单 CSV”，逐条比对清单声明信息与磁盘真实文件，判定每条
记录属于以下三类之一：

* ``存在风险``：路径穿越（含绝对路径、符号链接/目录联接逃逸）或扩展名伪装；
* ``安全``：路径位于上传目录内、文件存在且可读、扩展名与声明一致；
* ``无法判定``：文件缺失、不可读、是目录、清单行格式错误等。

工具**只读取文件内容与元数据**，任何上传文件都不会被执行，也不会被 import。

退出码
------
0  正常完成（默认即使存在风险也返回 0，便于修复后复跑对比）
1  指定 --fail-on-risk 且发现风险
2  命令行用法错误（缺少/多余参数、参数组合不完整）
3  报告完整性校验失败（计数与逐条结果不一致），且不覆盖旧报告
4  上传目录不存在或不是目录，不生成报告
5  清单文件错误（缺失、非 UTF-8、缺少必需列、空清单）
6  写出报告时的 I/O 错误
"""

from __future__ import annotations

import argparse
import csv
import json
import ntpath
import os
import sys
import tempfile
from collections import Counter

TOOL_NAME = "upload-audit"
TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = 1

CATEGORY_RISK = "存在风险"
CATEGORY_SAFE = "安全"
CATEGORY_UNKNOWN = "无法判定"
CATEGORIES = (CATEGORY_RISK, CATEGORY_SAFE, CATEGORY_UNKNOWN)

RISK_PATH_TRAVERSAL = "path_traversal"
RISK_EXTENSION_DISGUISE = "extension_disguise"
RISK_TYPE_LABELS = {
    RISK_PATH_TRAVERSAL: "路径穿越",
    RISK_EXTENSION_DISGUISE: "扩展名伪装",
}

EXIT_OK = 0
EXIT_RISK_FOUND = 1
EXIT_USAGE = 2
EXIT_INTEGRITY = 3
EXIT_ENV = 4
EXIT_MANIFEST = 5
EXIT_IO = 6

REQUIRED_COLUMNS = ("relative_path", "declared_ext")
HEAD_BYTES = 4096


# --------------------------------------------------------------------------- #
# 清单读取
# --------------------------------------------------------------------------- #
class ManifestRow:
    """一行非空清单数据。"""

    __slots__ = ("raw_path", "declared_ext", "line_no", "index", "error")

    def __init__(self, raw_path, declared_ext, line_no, index, error=None):
        self.raw_path = raw_path
        self.declared_ext = declared_ext
        self.line_no = line_no
        self.index = index
        self.error = error


def _row_is_blank(row):
    return not row or all(not (cell or "").strip() for cell in row)


def read_manifest(path):
    """读取清单 CSV，返回 (rows, meta)。

    表头缺失必需列时抛出 ValueError（调用方转为退出码 5）。
    空行跳过；列数异常的行带 ``error`` 标记保留，稍后记为“无法判定”。
    """
    try:
        handle = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ValueError(f"无法打开清单文件: {exc}") from exc

    parsed = []
    blank_rows = 0
    header = None
    idx_path = idx_ext = None
    with handle:
        reader = csv.reader(handle, strict=False)
        try:
            for row in reader:
                if _row_is_blank(row):
                    blank_rows += 1
                    continue
                if header is None:
                    header = [cell.strip() for cell in row]
                    missing = [name for name in REQUIRED_COLUMNS if name not in header]
                    if missing:
                        raise ValueError(
                            "清单表头缺少必需列: " + ", ".join(missing)
                            + "（实际表头: " + ", ".join(header) + "）"
                        )
                    idx_path = header.index("relative_path")
                    idx_ext = header.index("declared_ext")
                    continue

                index = len(parsed) + 1
                if len(row) != len(header):
                    # 列数异常：尽力保留原始列值，判定为“无法判定”并注明原因
                    parsed.append(
                        ManifestRow(
                            raw_path=row[idx_path] if idx_path < len(row) else "",
                            declared_ext=row[idx_ext] if idx_ext < len(row) else "",
                            line_no=reader.line_num,
                            index=index,
                            error=(
                                f"清单行列数异常：表头 {len(header)} 列，"
                                f"本行 {len(row)} 列"
                            ),
                        )
                    )
                    continue

                parsed.append(
                    ManifestRow(
                        raw_path=row[idx_path],
                        declared_ext=row[idx_ext],
                        line_no=reader.line_num,
                        index=index,
                    )
                )
        except UnicodeDecodeError as exc:
            raise ValueError(f"清单不是合法 UTF-8 编码: {exc}") from exc
        except csv.Error as exc:
            raise ValueError(
                f"清单 CSV 解析失败（第 {reader.line_num} 行）: {exc}"
            ) from exc

    if header is None:
        raise ValueError("清单为空（没有表头行）")

    meta = {
        "header": header,
        "data_rows": len(parsed),
        "blank_rows_skipped": blank_rows,
    }
    return parsed, meta


# --------------------------------------------------------------------------- #
# 路径与判定
# --------------------------------------------------------------------------- #
def _is_within(root_real, candidate_real):
    """候选真实路径是否位于 root 真实路径之下（按平台大小写语义）。"""
    try:
        root = os.path.normcase(root_real)
        cand = os.path.normcase(candidate_real)
        return os.path.commonpath([root, cand]) == root
    except ValueError:
        return False  # 不同驱动器等无法比较的情况


def normalize_relative(raw_path):
    """把清单路径按 ``/`` 与 ``\\\\`` 双分隔符做词法规范化。

    返回 ``(segments, escape_kind)``：

    * ``segments``：消除 ``.`` 与可消解的 ``..`` 之后的路径段；
    * ``escape_kind``：``None`` 表示未越界；``"absolute"`` 表示绝对路径 /
      盘符 / UNC / 根路径；``"parent"`` 表示 ``..`` 越过上传目录根。
    """
    text = raw_path.replace("\\", "/")
    if ntpath.splitdrive(text)[0] or text.startswith("/"):
        return [], "absolute"

    segments = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not segments:
                return [], "parent"
            segments.pop()
        else:
            segments.append(part)
    return segments, None


def _normalize_declared_ext(value):
    text = (value or "").strip()
    if not text:
        return ""
    if not text.startswith("."):
        text = "." + text
    return text.lower()


def _new_record(row):
    record = {
        "index": row.index,
        "manifest_line": row.line_no,
        "relative_path": row.raw_path,
        "normalized_path": None,
        "declared_ext": row.declared_ext,
        "actual_ext": None,
        "status": CATEGORY_UNKNOWN,
        "risk_types": [],
        "reason_code": None,
        "reasons": [],
        "resolved_path": None,
        "size_bytes": None,
    }
    if row.error:
        record["reason_code"] = "malformed_row"
        record["reasons"].append(row.error)
    return record


def _decide(record, status, code, reason, risk_type=None):
    record["status"] = status
    record["reason_code"] = code
    record["reasons"].append(reason)
    if risk_type is not None and risk_type not in record["risk_types"]:
        record["risk_types"].append(risk_type)


def _read_head(path):
    """只读取文件头部若干字节以确认可读性；绝不执行文件。"""
    with open(path, "rb") as handle:
        return handle.read(HEAD_BYTES)


def classify(upload_real, row):
    """对单条清单记录做判定，返回结果字典。"""
    record = _new_record(row)
    if row.error:
        return record

    raw_path = (row.raw_path or "").strip()
    declared_ext = (row.declared_ext or "").strip()

    if not raw_path:
        _decide(record, CATEGORY_UNKNOWN, "empty_path", "relative_path 为空")
        return record
    if "\x00" in raw_path:
        _decide(
            record, CATEGORY_RISK, "nul_byte",
            "relative_path 含 NUL 字符", RISK_PATH_TRAVERSAL,
        )
        return record

    segments, escape_kind = normalize_relative(raw_path)
    if escape_kind == "absolute":
        record["normalized_path"] = raw_path.replace("\\", "/")
        _decide(
            record, CATEGORY_RISK, "absolute_path",
            f"relative_path 是绝对路径（{raw_path}），不受上传目录约束",
            RISK_PATH_TRAVERSAL,
        )
        return record
    if escape_kind == "parent":
        record["normalized_path"] = ntpath.normpath(raw_path.replace("/", "\\")).replace("\\", "/")
        _decide(
            record, CATEGORY_RISK, "path_traversal",
            f"relative_path 规范化后跳出上传目录（{raw_path}）",
            RISK_PATH_TRAVERSAL,
        )
        return record

    normalized = "/".join(segments) if segments else "."
    record["normalized_path"] = normalized

    # 词法上的路径（保留 .. 原样），用于符号链接感知的真实路径解析：
    # OS 会先跟随链接再处理 ..，与纯词法规范化可能不同。
    raw_candidate = os.path.join(upload_real, *raw_path.replace("\\", "/").split("/"))
    lexical_candidate = os.path.join(upload_real, *segments) if segments else upload_real
    record["resolved_path"] = lexical_candidate

    for candidate in (raw_candidate, lexical_candidate):
        try:
            real_candidate = os.path.realpath(candidate)
        except OSError as exc:
            _decide(
                record, CATEGORY_UNKNOWN, "unreadable",
                f"无法解析真实路径: {exc}",
            )
            return record
        if not _is_within(upload_real, real_candidate):
            _decide(
                record, CATEGORY_RISK, "symlink_escape",
                f"符号链接/目录联接指向上传目录之外（{real_candidate}）",
                RISK_PATH_TRAVERSAL,
            )
            return record

    candidate = lexical_candidate
    if not os.path.lexists(candidate):
        _decide(record, CATEGORY_UNKNOWN, "missing_file", "磁盘上不存在该文件")
        return record
    if not os.path.exists(candidate):
        _decide(
            record, CATEGORY_UNKNOWN, "broken_symlink",
            "符号链接目标不存在（悬空链接），无法读取",
        )
        return record
    if os.path.isdir(candidate):
        _decide(record, CATEGORY_UNKNOWN, "is_directory", "磁盘上的目标是目录，不是文件")
        return record
    if not os.path.isfile(candidate):
        _decide(
            record, CATEGORY_UNKNOWN, "not_regular_file",
            "磁盘上的目标不是普通文件",
        )
        return record

    try:
        stat = os.stat(candidate)
        _read_head(candidate)
    except OSError as exc:
        _decide(record, CATEGORY_UNKNOWN, "unreadable", f"无法读取文件: {exc}")
        return record

    record["size_bytes"] = stat.st_size

    base_name = os.path.basename(normalized)
    actual_ext = os.path.splitext(base_name)[1].lower()
    record["actual_ext"] = actual_ext
    normalized_declared = _normalize_declared_ext(declared_ext)

    if not normalized_declared:
        _decide(
            record, CATEGORY_UNKNOWN, "declared_ext_empty",
            "declared_ext 为空，无法比较扩展名",
        )
        return record

    if not _extension_matches(base_name, normalized_declared):
        record["risk_types"].append(RISK_EXTENSION_DISGUISE)
        _decide(
            record, CATEGORY_RISK, "extension_disguise",
            f"扩展名伪装：清单声明 {declared_ext!r}，磁盘实际 {actual_ext!r}",
        )
        return record

    _decide(record, CATEGORY_SAFE, "ok", "路径位于上传目录内，扩展名与声明一致")
    return record


def _extension_matches(base_name, declared_ext):
    """大小写不敏感地判断文件名是否以声明的扩展名结尾。

    支持 ``.tar.gz`` 这类复合扩展名；``payload.exe`` 声明 ``.png`` 不匹配，
    声明 ``.exe`` 匹配；``photo.png`` 与 ``.PNG`` 视为一致。
    """
    if any(sep in declared_ext for sep in ("/", "\\")):
        return False
    return base_name.lower().endswith(declared_ext)


def scan(upload_dir, manifest_path):
    """执行扫描并返回报告字典。上传目录有效性由调用方预先校验。

    扫描全程只做 ``stat`` 与读取文件头，不执行、不导入任何上传文件。
    """
    upload_real = os.path.realpath(os.path.abspath(upload_dir))
    rows, meta = read_manifest(manifest_path)

    results = [classify(upload_real, row) for row in rows]
    # 稳定排序：同 relative_path 保持清单原始出现顺序（Python sort 稳定）。
    results.sort(key=lambda item: item["relative_path"])

    counts = {category: 0 for category in CATEGORIES}
    for item in results:
        counts[item["status"]] += 1
    counts["total"] = len(results)

    report = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "report_type": "scan",
        "upload_dir": os.path.abspath(upload_dir),
        "manifest": os.path.abspath(manifest_path),
        "categories": list(CATEGORIES),
        "risk_type_labels": dict(RISK_TYPE_LABELS),
        "counts": counts,
        "results": results,
        "manifest_meta": meta,
    }
    return report


# --------------------------------------------------------------------------- #
# 报告校验与写出
# --------------------------------------------------------------------------- #
def validate_report(data):
    """校验报告内部一致性，返回错误信息列表（空列表表示通过）。

    支持两种 report_type：``scan``（逐条结果 + 三类计数）与 ``comparison``
    （新增风险 / 已消除风险两组条目 + 汇总计数）。
    """
    if not isinstance(data, dict):
        return ["报告根节点必须是 JSON 对象"]
    report_type = data.get("report_type")
    if report_type == "comparison":
        return _validate_comparison(data)
    if report_type != "scan":
        return [f"未知的报告类型（report_type={report_type!r}）"]

    errors = []
    results = data.get("results")
    counts = data.get("counts")
    if not isinstance(results, list):
        errors.append("缺少 results 数组")
        results = []
    if not isinstance(counts, dict):
        errors.append("缺少 counts 对象")
        counts = {}

    for category in CATEGORIES:
        if category not in counts:
            errors.append(f"counts 缺少字段: {category}")
        elif isinstance(counts[category], bool) or not isinstance(counts[category], int):
            errors.append(f"counts.{category} 必须是整数")
    if "total" not in counts:
        errors.append("counts 缺少字段: total")
    elif isinstance(counts["total"], bool) or not isinstance(counts["total"], int):
        errors.append("counts.total 必须是整数")

    actual = Counter()
    for position, item in enumerate(results):
        if not isinstance(item, dict):
            errors.append(f"results[{position}] 不是对象")
            continue
        if not isinstance(item.get("relative_path"), str):
            errors.append(f"results[{position}] 缺少 relative_path")
        status = item.get("status")
        if status not in CATEGORIES:
            errors.append(f"results[{position}].status 非法: {status!r}")
            continue
        actual[status] += 1
        if status == CATEGORY_RISK and not item.get("risk_types"):
            errors.append(f"results[{position}] 标记为风险但未给出 risk_types")
        if status == CATEGORY_UNKNOWN and not item.get("reasons"):
            errors.append(f"results[{position}] 标记为无法判定但未给出 reasons")

    if not errors:
        for category in CATEGORIES:
            if counts.get(category) != actual[category]:
                errors.append(
                    f"counts.{category}={counts.get(category)!r} 与逐条统计 "
                    f"{actual[category]} 不一致"
                )
        if counts.get("total") != len(results):
            errors.append(
                f"counts.total={counts.get('total')!r} 与 results 长度 "
                f"{len(results)} 不一致"
            )
        if counts.get("total") != sum(actual.values()):
            errors.append("counts.total 不等于三类计数之和")

    return errors


def _validate_comparison(data):
    """校验对比报告：两组条目与 summary 计数一致，且无未知 risk_type。"""
    errors = []
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return ["缺少 summary 对象"]

    groups = {}
    for name in ("新增风险", "已消除风险"):
        entries = data.get(name)
        if not isinstance(entries, list):
            errors.append(f"缺少 {name} 数组")
            entries = []
        groups[name] = entries
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"{name}[{position}] 不是对象")
                continue
            if entry.get("risk_type") not in RISK_TYPE_LABELS:
                errors.append(
                    f"{name}[{position}].risk_type 非法: {entry.get('risk_type')!r}"
                )
            if not isinstance(entry.get("relative_path"), str):
                errors.append(f"{name}[{position}] 缺少 relative_path")

    if not errors:
        if summary.get("new_risks") != len(groups["新增风险"]):
            errors.append("summary.new_risks 与 新增风险 数组长度不一致")
        if summary.get("resolved_risks") != len(groups["已消除风险"]):
            errors.append("summary.resolved_risks 与 已消除风险 数组长度不一致")
        for key in ("baseline_risks", "current_risks"):
            if isinstance(summary.get(key), bool) or not isinstance(summary.get(key), int):
                errors.append(f"summary.{key} 必须是整数")
    return errors


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _existing_report_problem(path):
    """目标已存在且是“形似报告但内部不一致”的 JSON 时，返回问题列表。"""
    if not os.path.exists(path):
        return None
    if os.path.isdir(path):
        return ["目标路径是目录，无法写入报告"]
    try:
        data = _load_json(path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None  # 不是可解析 JSON，可以安全覆盖
    if not isinstance(data, dict) or "report_type" not in data:
        return None
    errors = validate_report(data)
    return errors or None


def write_json_report(path, data, *, guard_existing=True):
    """原子写出 JSON 报告，返回 ``(是否成功, 错误列表)``。

    guard_existing=True 时，若目标位置已有计数不一致的报告，则拒绝覆盖，
    以保证“篡改后重跑不破坏原报告”。
    """
    if guard_existing:
        problem = _existing_report_problem(path)
        if problem:
            return False, ["拒绝覆盖已存在的损坏报告：", *problem]

    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".upload-audit-", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        return False, [f"写出报告失败: {exc}"]
    return True, []


# --------------------------------------------------------------------------- #
# 对比
# --------------------------------------------------------------------------- #
def _risk_entries(report):
    """把报告中的风险展开为可比较条目（每个 risk_type 一条，重复出现带序号）。"""
    entries = []
    occurrences = Counter()
    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(results, list):
        return entries

    for item in results:
        if not isinstance(item, dict) or item.get("status") != CATEGORY_RISK:
            continue
        path = item.get("normalized_path") or item.get("relative_path") or ""
        path_key = os.path.normcase(path.replace("\\", "/"))
        ext_key = _normalize_declared_ext(item.get("declared_ext"))
        for risk_type in sorted(item.get("risk_types") or []):
            base = (path_key, ext_key, risk_type)
            occurrences[base] += 1
            entries.append(
                {
                    "key": base + (occurrences[base],),
                    "relative_path": item.get("relative_path"),
                    "normalized_path": item.get("normalized_path"),
                    "declared_ext": item.get("declared_ext"),
                    "actual_ext": item.get("actual_ext"),
                    "risk_type": risk_type,
                    "risk_label": RISK_TYPE_LABELS.get(risk_type, risk_type),
                    "reasons": list(item.get("reasons") or []),
                }
            )
    return entries


def _public_entry(entry):
    return {key: value for key, value in entry.items() if key != "key"}


def compare_reports(baseline, current, baseline_path, current_path):
    before = _risk_entries(baseline)
    after = _risk_entries(current)
    before_keys = Counter(entry["key"] for entry in before)
    after_keys = Counter(entry["key"] for entry in after)

    new_risks = []
    for entry in after:
        if before_keys[entry["key"]] > 0:
            before_keys[entry["key"]] -= 1
        else:
            new_risks.append(_public_entry(entry))

    resolved = []
    for entry in before:
        if after_keys[entry["key"]] > 0:
            after_keys[entry["key"]] -= 1
        else:
            resolved.append(_public_entry(entry))

    return {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "report_type": "comparison",
        "baseline_report": os.path.abspath(baseline_path),
        "current_report": os.path.abspath(current_path),
        "risk_type_labels": dict(RISK_TYPE_LABELS),
        "summary": {
            "baseline_risks": len(before),
            "current_risks": len(after),
            "new_risks": len(new_risks),
            "resolved_risks": len(resolved),
        },
        "新增风险": new_risks,
        "已消除风险": resolved,
    }


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _configure_stdio():
    """尽量让中文在 Windows 控制台/重定向下正确输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser():
    parser = argparse.ArgumentParser(
        prog="upload_audit.py",
        description=(
            "上传目录落盘核验：比对清单声明与磁盘真实文件，判定 "
            "存在风险 / 安全 / 无法判定。仅读取内容与元数据，绝不执行上传文件。"
        ),
        epilog=(
            "示例:\n"
            "  python -I upload_audit.py ./upload ./manifest.csv --report ./before.json\n"
            "  python -I upload_audit.py ./upload_fixed ./manifest_fixed.csv "
            "--report ./after.json --baseline ./before.json --compare-report ./compare.json\n"
            "  python -I upload_audit.py --validate-report ./before.json\n"
            "\n退出码: 0 成功, 1 发现风险(--fail-on-risk), 2 用法错误, "
            "3 报告校验失败, 4 上传目录无效, 5 清单错误, 6 写出失败"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("upload_dir", nargs="?", help="上传目录")
    parser.add_argument("manifest", nargs="?", help="清单 CSV 路径（UTF-8）")
    parser.add_argument("--report", metavar="PATH", help="扫描结果 JSON 报告输出路径")
    parser.add_argument(
        "--baseline", metavar="PATH", help="修复前的扫描报告（需同时给 --compare-report）"
    )
    parser.add_argument(
        "--compare-report", metavar="PATH",
        help="对比报告输出路径，包含 新增风险 / 已消除风险",
    )
    parser.add_argument(
        "--validate-report", metavar="PATH",
        help="仅校验既有报告；计数与逐条结果不一致时退出码 3，不写任何文件",
    )
    parser.add_argument(
        "--fail-on-risk", action="store_true", help="发现任一风险时退出码 1"
    )
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help="即使目标位置已存在计数不一致的报告也覆盖（默认拒绝）",
    )
    parser.add_argument(
        "--timestamp", metavar="ISO8601",
        help="写入报告的生成时间（默认不写，保证输出可复现）",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {TOOL_VERSION}"
    )
    return parser


def _usage_error(parser, message):
    parser.print_usage(sys.stderr)
    print(f"错误：{message}", file=sys.stderr)
    return EXIT_USAGE


def _print_scan_summary(report):
    counts = report["counts"]
    print(
        "扫描完成：共 {total} 条；存在风险 {risk}；安全 {safe}；无法判定 {unknown}".format(
            total=counts["total"],
            risk=counts[CATEGORY_RISK],
            safe=counts[CATEGORY_SAFE],
            unknown=counts[CATEGORY_UNKNOWN],
        )
    )


def _validate_existing(path):
    try:
        data = _load_json(path)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, [f"无法读取报告 {path}: {exc}"]
    return data, validate_report(data)


def cmd_validate(path):
    data, errors = _validate_existing(path)
    if errors:
        print(f"报告 {path} 校验失败：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_INTEGRITY

    if data.get("report_type") == "comparison":
        summary = data["summary"]
        print(
            "对比报告 {path} 校验通过：新增风险 {new} 条；已消除风险 {resolved} 条；"
            "基线风险 {base}；当前风险 {cur}".format(
                path=path,
                new=summary["new_risks"],
                resolved=summary["resolved_risks"],
                base=summary["baseline_risks"],
                cur=summary["current_risks"],
            )
        )
        return EXIT_OK

    counts = data["counts"]
    print(
        "报告 {path} 校验通过：存在风险 {risk}；安全 {safe}；"
        "无法判定 {unknown}；总计 {total}".format(
            path=path,
            risk=counts[CATEGORY_RISK],
            safe=counts[CATEGORY_SAFE],
            unknown=counts[CATEGORY_UNKNOWN],
            total=counts["total"],
        )
    )
    return EXIT_OK


def cmd_compare(args, current_report):
    baseline, errors = _validate_existing(args.baseline)
    if errors:
        print(f"基线报告 {args.baseline} 无法用于对比：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_INTEGRITY
    if baseline.get("report_type") != "scan":
        print(
            f"错误：基线必须是扫描报告，实际为 {baseline.get('report_type')!r}: "
            f"{args.baseline}",
            file=sys.stderr,
        )
        return EXIT_INTEGRITY
    if os.path.abspath(args.compare_report) in {
        os.path.abspath(args.baseline),
        os.path.abspath(args.report),
    }:
        print(
            "错误：--compare-report 不能与基线或当前扫描报告使用同一路径",
            file=sys.stderr,
        )
        return EXIT_USAGE

    comparison = compare_reports(
        baseline, current_report, args.baseline, args.report
    )
    if args.timestamp:
        comparison["generated_at"] = args.timestamp

    ok, errors = write_json_report(
        args.compare_report, comparison,
        guard_existing=not args.force_overwrite,
    )
    if not ok:
        print("错误：未能写出对比报告：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_INTEGRITY if errors and errors[0].startswith("拒绝覆盖") else EXIT_IO

    summary = comparison["summary"]
    print(
        "对比完成：新增风险 {new} 条；已消除风险 {resolved} 条；对比报告 {path}".format(
            new=summary["new_risks"],
            resolved=summary["resolved_risks"],
            path=os.path.abspath(args.compare_report),
        )
    )
    return EXIT_OK


def main(argv=None):
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.validate_report:
        return cmd_validate(args.validate_report)

    if not args.upload_dir or not args.manifest:
        return _usage_error(parser, "必须同时提供 上传目录 与 清单CSV 路径")
    if not args.report:
        return _usage_error(parser, "必须通过 --report 指定报告输出路径")
    if (args.baseline is None) != (args.compare_report is None):
        return _usage_error(parser, "--baseline 与 --compare-report 必须同时提供")

    upload_dir = args.upload_dir
    if not os.path.exists(upload_dir):
        print(f"错误：上传目录不存在: {os.path.abspath(upload_dir)}", file=sys.stderr)
        return EXIT_ENV
    if not os.path.isdir(upload_dir):
        print(f"错误：上传目录不是目录: {os.path.abspath(upload_dir)}", file=sys.stderr)
        return EXIT_ENV

    if not os.path.isfile(args.manifest):
        print(
            f"错误：清单文件不存在或不是普通文件: {os.path.abspath(args.manifest)}",
            file=sys.stderr,
        )
        return EXIT_MANIFEST

    # 防止把报告写到清单/基线所在位置，破坏输入
    protected = {os.path.abspath(args.manifest)}
    if args.baseline:
        protected.add(os.path.abspath(args.baseline))
    if os.path.abspath(args.report) in protected:
        print(
            "错误：--report 不能指向清单或基线报告文件，拒绝覆盖输入: "
            + os.path.abspath(args.report),
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        report = scan(upload_dir, args.manifest)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_MANIFEST

    if args.timestamp:
        report["generated_at"] = args.timestamp

    # 先自检，避免写出内部不一致的报告
    errors = validate_report(report)
    if errors:
        print("内部错误：生成的报告未通过一致性校验：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_INTEGRITY

    ok, errors = write_json_report(
        args.report, report, guard_existing=not args.force_overwrite
    )
    if not ok:
        print("错误：未能写出报告：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_INTEGRITY if errors and errors[0].startswith("拒绝覆盖") else EXIT_IO

    _print_scan_summary(report)
    print(f"报告：{os.path.abspath(args.report)}")

    if args.baseline and args.compare_report:
        code = cmd_compare(args, report)
        if code != EXIT_OK:
            return code

    if args.fail_on_risk and report["counts"][CATEGORY_RISK] > 0:
        return EXIT_RISK_FOUND
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
