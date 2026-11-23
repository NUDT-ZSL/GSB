"""记忆压缩 DAG 编排：摄入校验 -> 窗口切分 -> 窗口摘要 -> 条目合并与链引用 -> 打分裁剪。

同时承担三类节点的内容寻址缓存读写与重算/复用统计。
"""

import json
from dataclasses import dataclass, field

from .cache import entry_cache_key, summary_cache_key, window_cache_key
from .merge import (
    attach_chain_refs,
    build_chain_refs,
    build_entries_for_session,
    group_windows,
)
from .scoring import prune_to_budget, score_entries
from .vocab import controlled_vocab
from .window import split_windows, summarize_window


@dataclass
class PipelineResult:
    windows: dict = field(default_factory=dict)
    summaries: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)
    chain_refs: dict = field(default_factory=dict)
    score_report: dict = field(default_factory=dict)
    pruning: dict = field(default_factory=dict)
    cache_report: dict = field(default_factory=dict)


def run_pipeline(sessions, spec, cache=None, vocab=None):
    vocab = vocab or controlled_vocab()
    result = PipelineResult()
    cache_report = {
        "windows": {"recompute": [], "reuse": []},
        "summaries": {"recompute": [], "reuse": []},
        "entries": {"recompute": [], "reuse": []},
    }
    all_entries = []
    entry_id = 1
    for session_id in sorted(sessions):
        session = sessions[session_id]
        windows = split_windows(session, spec)
        result.windows[session_id] = windows
        window_keys = {}
        for w in windows:
            wkey = window_cache_key(w, spec)
            window_keys[w.window_id] = wkey
            if cache is not None and cache.has("windows", wkey):
                cache_report["windows"]["reuse"].append(w.window_id)
            else:
                cache_report["windows"]["recompute"].append(w.window_id)
                if cache is not None:
                    cache.put("windows", wkey, json.dumps(w.to_dict(), sort_keys=True))
        summaries = {}
        summary_keys = {}
        for w in windows:
            wkey = window_keys[w.window_id]
            skey = summary_cache_key(wkey, spec)
            summary_keys[w.window_id] = skey
            if cache is not None and cache.has("summaries", skey):
                cached = json.loads(cache.get("summaries", skey).decode("utf-8"))
                summaries[w.window_id] = tuple(cached["summary_tokens"])
                cache_report["summaries"]["reuse"].append(w.window_id)
            else:
                tokens = summarize_window(w, spec, vocab)
                summaries[w.window_id] = tokens
                cache_report["summaries"]["recompute"].append(w.window_id)
                if cache is not None:
                    cache.put(
                        "summaries",
                        skey,
                        json.dumps({"window_id": w.window_id, "summary_tokens": list(tokens)}),
                    )
        result.summaries.update(summaries)
        entries = build_entries_for_session(session, windows, spec, vocab, entry_id)
        entry_id += len(entries)
        groups = group_windows(windows, spec)
        for entry, group in zip(entries, groups):
            ekey = entry_cache_key([summary_keys[wid] for wid in group], spec)
            if cache is not None and cache.has("entries", ekey):
                cache_report["entries"]["reuse"].append(entry.entry_id)
            else:
                cache_report["entries"]["recompute"].append(entry.entry_id)
                if cache is not None:
                    cache.put("entries", ekey, json.dumps({"entry_id": entry.entry_id}))
        all_entries.extend(entries)
    refs = build_chain_refs(all_entries, spec.chain_threshold)
    all_entries = attach_chain_refs(all_entries, refs)
    result.entries = all_entries
    result.chain_refs = refs
    result.score_report = score_entries(all_entries, sessions)
    result.pruning = prune_to_budget(all_entries, result.score_report, spec.budget_tokens)
    result.cache_report = cache_report
    return result
