"""v1/v2/v3 三种记忆条目持久化格式的读取器与写出器。

分派规则：优先按格式头 `#memfmt:vN`；无格式头时按字段集嗅探
（JSON 对象 -> v3；key=value 字段 -> v2；否则 -> v1）。
"""

import json
import re

from .errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, MemError
from .model import MemoryEntry, fmt_float

HEADER_RE = re.compile(r"^#memfmt:v(\d+)\s*$")
SUPPORTED_VERSIONS = (1, 2, 3)

V2_REQUIRED_FIELDS = ("id", "session", "turns", "sources", "summary", "conf", "imp")
V3_REQUIRED_FIELDS = (
    "entry_id",
    "session_id",
    "turn_range",
    "source_turns",
    "summary_tokens",
    "confidence",
    "importance",
    "chain_refs",
    "merge_sources",
    "revision",
)


def detect_version(first_line, path):
    match = HEADER_RE.match(first_line)
    if match:
        version = int(match.group(1))
    else:
        stripped = first_line.strip()
        if stripped.startswith("{"):
            version = 3
        elif "=" in stripped.split("|", 1)[0]:
            version = 2
        else:
            version = 1
    if version not in SUPPORTED_VERSIONS:
        raise MemError(
            E_VERSION_UNSUPPORTED,
            f"不支持的记忆条目格式版本 v{version}（{path}）",
        )
    return version


def _corrupt(message, session_id, line_no):
    return MemError(E_FORMAT_CORRUPT, message, session_id=session_id, line_no=line_no)


def _parse_turn_range(text, session_id, line_no):
    parts = text.split("-")
    if len(parts) != 2:
        raise _corrupt(f"轮次范围无法解析: {text!r}", session_id, line_no)
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        raise _corrupt(f"轮次范围数值不可解析: {text!r}", session_id, line_no)
    if start > end:
        raise _corrupt(f"轮次范围起点大于终点: {text!r}", session_id, line_no)
    return start, end


def _parse_int_list(text, session_id, line_no):
    if not text:
        return ()
    try:
        return tuple(int(x) for x in text.split(","))
    except ValueError:
        raise _corrupt(f"整数列表不可解析: {text!r}", session_id, line_no)


def _parse_token_list(text, session_id, line_no):
    tokens = tuple(t for t in text.split(",") if t)
    if not tokens:
        raise _corrupt("摘要 token 列表为空", session_id, line_no)
    return tokens


def _parse_float(text, session_id, line_no):
    try:
        return float(text)
    except ValueError:
        raise _corrupt(f"数值不可解析: {text!r}", session_id, line_no)


def parse_v1_line(line, line_no):
    parts = line.split("|")
    session_id = parts[0] if parts and parts[0] else None
    if len(parts) != 4:
        raise _corrupt(
            f"v1 行字段数不符：期望 4 个竖线分列字段，实际 {len(parts)} 个",
            session_id,
            line_no,
        )
    session_id, turn_range, summary, importance = parts
    if not session_id:
        raise _corrupt("v1 行缺少 session_id", session_id, line_no)
    start, end = _parse_turn_range(turn_range, session_id, line_no)
    return MemoryEntry(
        entry_id=0,
        session_id=session_id,
        turn_start=start,
        turn_end=end,
        summary_tokens=_parse_token_list(summary, session_id, line_no),
        importance=_parse_float(importance, session_id, line_no),
    )


def parse_v2_line(line, line_no):
    fields = {}
    for chunk in line.split("|"):
        if "=" not in chunk:
            raise _corrupt(f"v2 字段缺少 '=': {chunk!r}", None, line_no)
        name, value = chunk.split("=", 1)
        fields[name.strip()] = value.strip()
    session_id = fields.get("session")
    missing = [k for k in V2_REQUIRED_FIELDS if k not in fields]
    if missing:
        raise _corrupt(f"v2 行必填字段缺失: {missing}", session_id, line_no)
    start, end = _parse_turn_range(fields["turns"], session_id, line_no)
    conf_parts = fields["conf"].split("..")
    if len(conf_parts) != 2:
        raise _corrupt(f"置信区间格式错误: {fields['conf']!r}", session_id, line_no)
    try:
        entry_id = int(fields["id"])
    except ValueError:
        raise _corrupt(f"条目序号不可解析: {fields['id']!r}", session_id, line_no)
    return MemoryEntry(
        entry_id=entry_id,
        session_id=session_id,
        turn_start=start,
        turn_end=end,
        source_turns=_parse_int_list(fields["sources"], session_id, line_no),
        summary_tokens=_parse_token_list(fields["summary"], session_id, line_no),
        conf_lo=_parse_float(conf_parts[0], session_id, line_no),
        conf_hi=_parse_float(conf_parts[1], session_id, line_no),
        importance=_parse_float(fields["imp"], session_id, line_no),
    )


def parse_v3_line(line, line_no):
    try:
        doc = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _corrupt(f"v3 JSON 不可解析: {exc}", None, line_no)
    if not isinstance(doc, dict):
        raise _corrupt("v3 行必须是 JSON 对象", None, line_no)
    session_id = doc.get("session_id")
    missing = [k for k in V3_REQUIRED_FIELDS if k not in doc]
    if missing:
        raise _corrupt(f"v3 行必填字段缺失: {missing}", session_id, line_no)
    try:
        turn_range = doc["turn_range"]
        confidence = doc["confidence"]
        return MemoryEntry(
            entry_id=int(doc["entry_id"]),
            session_id=str(doc["session_id"]),
            turn_start=int(turn_range[0]),
            turn_end=int(turn_range[1]),
            source_turns=tuple(int(x) for x in doc["source_turns"]),
            summary_tokens=tuple(str(t) for t in doc["summary_tokens"]),
            conf_lo=float(confidence["lo"]),
            conf_hi=float(confidence["hi"]),
            importance=float(doc["importance"]),
            chain_refs=tuple(int(x) for x in doc["chain_refs"]),
            merge_sources=tuple(str(x) for x in doc["merge_sources"]),
            revision=int(doc["revision"]),
        )
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise _corrupt(f"v3 字段数值不可解析: {exc}", session_id, line_no)


PARSERS = {1: parse_v1_line, 2: parse_v2_line, 3: parse_v3_line}


def load_entries(path):
    """按格式头或字段集分派解析，返回 (version, [MemoryEntry])。

    v1 读取结果中 v2/v3 专有字段保持缺省值，由迁移器补全。
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw_lines = [ln.rstrip("\n") for ln in fh]
    lines = [(i + 1, ln) for i, ln in enumerate(raw_lines) if ln.strip()]
    if not lines:
        raise MemError(E_FORMAT_CORRUPT, f"空文件: {path}")
    first_no, first_line = lines[0]
    header = HEADER_RE.match(first_line)
    version = detect_version(first_line, path)
    entries = []
    for line_no, line in lines:
        if HEADER_RE.match(line):
            continue
        entries.append(PARSERS[version](line, line_no))
    return version, entries


def format_v1_line(entry):
    return "|".join(
        [
            entry.session_id,
            f"{entry.turn_start}-{entry.turn_end}",
            ",".join(entry.summary_tokens),
            fmt_float(entry.importance),
        ]
    )


def format_v2_line(entry):
    return "|".join(
        [
            f"id={entry.entry_id}",
            f"session={entry.session_id}",
            f"turns={entry.turn_start}-{entry.turn_end}",
            f"sources={','.join(str(t) for t in entry.source_turns)}",
            f"summary={','.join(entry.summary_tokens)}",
            f"conf={fmt_float(entry.conf_lo)}..{fmt_float(entry.conf_hi)}",
            f"imp={fmt_float(entry.importance)}",
        ]
    )


def format_v3_line(entry):
    doc = {
        "entry_id": entry.entry_id,
        "session_id": entry.session_id,
        "turn_range": [entry.turn_start, entry.turn_end],
        "source_turns": list(entry.source_turns),
        "summary_tokens": list(entry.summary_tokens),
        "confidence": {"lo": float(fmt_float(entry.conf_lo)), "hi": float(fmt_float(entry.conf_hi))},
        "importance": float(fmt_float(entry.importance)),
        "chain_refs": list(entry.chain_refs),
        "merge_sources": list(entry.merge_sources),
        "revision": entry.revision,
    }
    return json.dumps(doc, sort_keys=True, ensure_ascii=True)


def write_entries(entries, path, version):
    formatters = {1: format_v1_line, 2: format_v2_line, 3: format_v3_line}
    if version not in formatters:
        raise MemError(E_VERSION_UNSUPPORTED, f"不支持的写出版本 v{version}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#memfmt:v{version}\n")
        for entry in entries:
            fh.write(formatters[version](entry) + "\n")
