"""追加轮次 / 修订轮次 / 调整窗口上限后的定向失效传播报告。"""

from .cache import window_cache_key
from .merge import group_windows
from .window import split_windows


def detect_changes(old_sessions, new_sessions, old_spec, new_spec):
    """识别变化类型：append_turns / revise_turn / window_param。"""
    changes = []
    if old_spec.param_version() != new_spec.param_version():
        changes.append({"type": "window_param", "detail": "窗口参数版本变化"})
    for session_id in sorted(new_sessions):
        if session_id not in old_sessions:
            changes.append({"type": "append_turns", "session_id": session_id,
                            "detail": "新会话"})
            continue
        old_turns = old_sessions[session_id]["turns"]
        new_turns = new_sessions[session_id]["turns"]
        common = min(len(old_turns), len(new_turns))
        revised = [
            new_turns[i]["turn_no"]
            for i in range(common)
            if old_turns[i]["text_tokens"] != new_turns[i]["text_tokens"]
        ]
        for turn_no in revised:
            changes.append({"type": "revise_turn", "session_id": session_id,
                            "turn_no": turn_no})
        if len(new_turns) > len(old_turns):
            changes.append({
                "type": "append_turns",
                "session_id": session_id,
                "from_turn": old_turns[-1]["turn_no"] + 1 if old_turns else 1,
                "to_turn": new_turns[-1]["turn_no"],
            })
    return changes


def invalidation_report(old_sessions, new_sessions, old_spec, new_spec):
    """计算受影响窗口与条目集合，输出重算与复用集合。"""
    changes = detect_changes(old_sessions, new_sessions, old_spec, new_spec)
    old_keys = set()
    for session_id, session in old_sessions.items():
        for w in split_windows(session, old_spec):
            old_keys.add(window_cache_key(w, old_spec))
    report = {
        "changes": changes,
        "affected_windows": [],
        "recompute_windows": [],
        "reuse_windows": [],
        "affected_entries": [],
        "recompute_entries": [],
        "reuse_entries": [],
    }
    entry_seq = 1
    for session_id in sorted(new_sessions):
        windows = split_windows(new_sessions[session_id], new_spec)
        affected_wids = []
        for w in windows:
            if window_cache_key(w, new_spec) in old_keys:
                report["reuse_windows"].append(w.window_id)
            else:
                report["recompute_windows"].append(w.window_id)
                affected_wids.append(w.window_id)
        report["affected_windows"].extend(affected_wids)
        groups = group_windows(windows, new_spec)
        for offset, group in enumerate(groups):
            eid = entry_seq + offset
            if any(wid in affected_wids for wid in group):
                report["affected_entries"].append(eid)
                report["recompute_entries"].append(eid)
            else:
                report["reuse_entries"].append(eid)
        entry_seq += len(groups)
    return report
