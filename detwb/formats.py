"""v1/v2/v3 三套匹配结果书写形式：按格式头分派的读取器与确定性写出器。

v1: 无字段名竖线分列行式文本（表达力受限）。
v2: 带字段名的行式文本（k=v 对，竖线分隔）。
v3: 结构化 JSON。
"""

import json

from .canon import canonical_json
from .errors import Err, WorkbenchError

V1_FIELDS = ["sample_id", "cand_idx", "x1", "y1", "x2", "y2",
             "score", "class_id", "gt_idx", "iou", "decision"]

RICH_FIELDS = ["sample_id", "cand_idx", "x1", "y1", "x2", "y2", "score",
               "class_id", "class_name", "gt_idx", "iou", "decision",
               "difficulty", "occluded", "conf_lo", "conf_hi", "vocab_version"]

INT_FIELDS = {"cand_idx", "class_id", "gt_idx", "vocab_version"}
FLOAT_FIELDS = {"x1", "y1", "x2", "y2", "score", "iou", "conf_lo", "conf_hi"}
BOOL_FIELDS = {"occluded"}

SUPPORTED_VERSIONS = ("v1", "v2", "v3")


def _fmt_float(v):
    return f"{float(v):.6f}"


def _fmt_value(field, value):
    if field in FLOAT_FIELDS:
        return _fmt_float(value)
    if field in BOOL_FIELDS:
        return "true" if value else "false"
    return str(value)


def _parse_int(field, raw, line_no):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise WorkbenchError(Err.FORMAT_CORRUPT,
                             f"字段 {field} 的整数值不可解析: {raw!r}",
                             line=line_no, field=field)


def _parse_float(field, raw, line_no):
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise WorkbenchError(Err.FORMAT_CORRUPT,
                             f"字段 {field} 的数值不可解析: {raw!r}",
                             line=line_no, field=field)


def _parse_bool(field, raw, line_no):
    if raw in ("true", "True", "1"):
        return True
    if raw in ("false", "False", "0"):
        return False
    raise WorkbenchError(Err.FORMAT_CORRUPT,
                         f"字段 {field} 的布尔值不可解析: {raw!r}",
                         line=line_no, field=field)


def _coerce(field, raw, line_no):
    if field in INT_FIELDS:
        return _parse_int(field, raw, line_no)
    if field in FLOAT_FIELDS:
        return _parse_float(field, raw, line_no)
    if field in BOOL_FIELDS:
        return _parse_bool(field, raw, line_no)
    return raw


# ---------------------------------------------------------------- writers

def write_v1(records, vocab_version):
    lines = [f"#fmt=v1|vocab={int(vocab_version)}"]
    for r in sorted(records, key=lambda r: (r["sample_id"], r["cand_idx"])):
        lines.append("|".join(_fmt_value(f, r[f]) for f in V1_FIELDS))
    return "\n".join(lines) + "\n"


def write_v2(records, vocab_version):
    lines = [f"#fmt=v2|vocab={int(vocab_version)}"]
    for r in sorted(records, key=lambda r: (r["sample_id"], r["cand_idx"])):
        rec = dict(r)
        rec["vocab_version"] = int(vocab_version)
        lines.append("|".join(f"{f}={_fmt_value(f, rec[f])}" for f in RICH_FIELDS))
    return "\n".join(lines) + "\n"


def write_v3(records, vocab_version):
    out_records = []
    for r in sorted(records, key=lambda r: (r["sample_id"], r["cand_idx"])):
        rec = {f: r[f] for f in RICH_FIELDS if f != "vocab_version"}
        rec["vocab_version"] = int(vocab_version)
        out_records.append(rec)
    doc = {"format": "v3", "vocab_version": int(vocab_version),
           "records": out_records}
    return canonical_json(doc) + "\n"


# ---------------------------------------------------------------- readers

def _parse_header(line):
    """解析格式头，返回 (version, vocab_version)。"""
    if not line.startswith("#fmt="):
        return None
    body = line[len("#fmt="):]
    parts = body.split("|")
    version = parts[0].strip()
    if version not in SUPPORTED_VERSIONS:
        raise WorkbenchError(Err.FORMAT_VERSION_UNSUPPORTED,
                             f"未知格式版本: {version!r}", line=1, field="header")
    vocab_version = None
    for p in parts[1:]:
        if p.startswith("vocab="):
            vocab_version = _parse_int("vocab", p[len("vocab="):], 1)
    if vocab_version is None:
        raise WorkbenchError(Err.FORMAT_CORRUPT,
                             "格式头截断：缺少 vocab= 段", line=1, field="header")
    return version, vocab_version


def _read_v1(lines, vocab_version):
    records = []
    for line_no, line in lines:
        cols = line.split("|")
        if len(cols) != len(V1_FIELDS):
            raise WorkbenchError(
                Err.FORMAT_CORRUPT,
                f"v1 行字段数不符：期望 {len(V1_FIELDS)}，实际 {len(cols)}",
                line=line_no, field=V1_FIELDS[min(len(cols), len(V1_FIELDS) - 1)])
        rec = {}
        for f, raw in zip(V1_FIELDS, cols):
            rec[f] = _coerce(f, raw, line_no)
        rec["vocab_version"] = vocab_version
        records.append(rec)
    return records


def _read_v2(lines, vocab_version):
    records = []
    for line_no, line in lines:
        rec = {}
        for pair in line.split("|"):
            if "=" not in pair:
                raise WorkbenchError(Err.FORMAT_CORRUPT,
                                     f"v2 行存在无字段名片段: {pair!r}",
                                     line=line_no, field=pair.split("=")[0])
            k, v = pair.split("=", 1)
            rec[k.strip()] = (_coerce(k.strip(), v, line_no)
                              if k.strip() in RICH_FIELDS else v)
        for f in RICH_FIELDS:
            if f == "vocab_version":
                rec.setdefault(f, vocab_version)
            elif f not in rec:
                raise WorkbenchError(Err.FORMAT_CORRUPT,
                                     f"v2 行缺少必填字段 {f}",
                                     line=line_no, field=f)
        records.append({f: rec[f] for f in RICH_FIELDS})
    return records


def _read_v3(text):
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkbenchError(Err.FORMAT_CORRUPT,
                             f"v3 JSON 不可解析: {exc.msg}",
                             line=exc.lineno, field="json")
    fmt = doc.get("format")
    if fmt != "v3":
        raise WorkbenchError(Err.FORMAT_VERSION_UNSUPPORTED,
                             f"未知格式版本: {fmt!r}", line=1, field="format")
    vocab_version = _parse_int("vocab_version", doc.get("vocab_version"), 1)
    records = []
    for idx, raw in enumerate(doc.get("records", []), start=1):
        rec = {}
        for f in RICH_FIELDS:
            if f == "vocab_version":
                rec[f] = int(raw.get(f, vocab_version))
                continue
            if f not in raw:
                raise WorkbenchError(Err.FORMAT_CORRUPT,
                                     f"v3 记录缺少必填字段 {f}",
                                     line=idx, field=f)
            v = raw[f]
            if f in INT_FIELDS:
                rec[f] = _parse_int(f, v, idx)
            elif f in FLOAT_FIELDS:
                rec[f] = _parse_float(f, v, idx)
            elif f in BOOL_FIELDS:
                rec[f] = bool(v)
            else:
                rec[f] = v
        records.append(rec)
    return records, vocab_version


def read(text):
    """统一读取入口：按格式头或字段集分派。返回 (records, vocab_version, fmt)。"""
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        raise WorkbenchError(Err.FORMAT_CORRUPT, "空输入：缺少格式头",
                             line=1, field="header")
    if first.startswith("{"):
        records, vocab_version = _read_v3(text)
        return records, vocab_version, "v3"
    header = _parse_header(first)
    if header is None:
        raise WorkbenchError(Err.FORMAT_CORRUPT,
                             "格式头截断或缺失：首行应为 #fmt=vN|vocab=M",
                             line=1, field="header")
    version, vocab_version = header
    body = [(ln, l) for ln, l in enumerate(text.splitlines(), start=1)
            if ln > 1 and l.strip()]
    if version == "v1":
        return _read_v1(body, vocab_version), vocab_version, "v1"
    return _read_v2(body, vocab_version), vocab_version, "v2"
