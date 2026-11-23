"""记忆条目合并与记忆链引用节点。"""

from .errors import E_CHAIN_CYCLE, E_WINDOW_DUP_ENTRY, MemError
from .model import MemoryEntry
from .window import summarize_tokens

MERGE_VERSION = "merge-1.0"


def group_windows(windows, spec):
    """同一会话内相邻窗口按 entry_window_span 分组。"""
    groups = []
    for i in range(0, len(windows), spec.entry_window_span):
        groups.append(tuple(w.window_id for w in windows[i : i + spec.entry_window_span]))
    return groups


def validate_window_assignment(windows, groups):
    """任一窗口必须恰属于一个条目，否则 E_WINDOW_DUP_ENTRY。"""
    count = {}
    for group in groups:
        for wid in group:
            count[wid] = count.get(wid, 0) + 1
    for w in windows:
        n = count.get(w.window_id, 0)
        if n == 0:
            raise MemError(
                E_WINDOW_DUP_ENTRY, f"窗口 {w.window_id} 不属于任何记忆条目",
                session_id=w.session_id,
            )
        if n > 1:
            raise MemError(
                E_WINDOW_DUP_ENTRY, f"窗口 {w.window_id} 被分配到 {n} 个记忆条目",
                session_id=w.session_id,
            )


def build_entries_for_session(session, windows, spec, vocab, entry_id_start):
    """相邻窗口摘要按重叠 token 合并为记忆条目（对合并后的 token 流重新计频）。"""
    groups = group_windows(windows, spec)
    validate_window_assignment(windows, groups)
    by_id = {w.window_id: w for w in windows}
    entries = []
    entry_id = entry_id_start
    for group in groups:
        members = [by_id[wid] for wid in group]
        merged_tokens = tuple(tok for w in members for tok in w.tokens)
        summary = summarize_tokens(merged_tokens, spec, vocab)
        source_turns = tuple(sorted({t for w in members for t in w.turn_nos}))
        distinct_ratio = len(set(summary)) / max(1, len(summary))
        conf_lo = round(0.5 + 0.4 * distinct_ratio, 6)
        entries.append(
            MemoryEntry(
                entry_id=entry_id,
                session_id=session["session_id"],
                turn_start=min(source_turns),
                turn_end=max(source_turns),
                source_turns=source_turns,
                summary_tokens=summary,
                conf_lo=conf_lo,
                conf_hi=1.0,
                importance=0.0,
                chain_refs=(),
                merge_sources=tuple(group),
                revision=1,
            )
        )
        entry_id += 1
    return entries


def build_chain_refs(entries, threshold):
    """跨会话共享来源轮次数量达到门槛的条目间建立链引用。

    引用方向固定为序号大者引用序号小者，构成以条目序号为拓扑序的 DAG。
    返回 {entry_id: tuple(被引用 entry_id)}。
    """
    ordered = sorted(entries, key=lambda e: e.entry_id)
    refs = {e.entry_id: [] for e in ordered}
    for i, later in enumerate(ordered):
        later_turns = set(later.source_turns)
        for earlier in ordered[:i]:
            if earlier.session_id == later.session_id:
                continue
            shared = len(later_turns & set(earlier.source_turns))
            if shared >= threshold:
                refs[later.entry_id].append(earlier.entry_id)
    result = {k: tuple(v) for k, v in refs.items()}
    assert_acyclic(result)
    return result


def assert_acyclic(refs):
    """校验链引用不得成环，违反返回 E_CHAIN_CYCLE。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in refs}

    def visit(node, stack):
        color[node] = GRAY
        for nxt in refs.get(node, ()):
            if nxt not in color:
                color[nxt] = WHITE
            if color[nxt] == GRAY:
                cycle = "->".join(str(x) for x in stack + [node, nxt])
                raise MemError(E_CHAIN_CYCLE, f"记忆链引用成环: {cycle}")
            if color[nxt] == WHITE:
                visit(nxt, stack + [node])
        color[node] = BLACK

    for node in refs:
        if color[node] == WHITE:
            visit(node, [])


def attach_chain_refs(entries, refs):
    return [e.with_fields(chain_refs=tuple(refs.get(e.entry_id, ()))) for e in entries]
