"""确定性本地样本生成器（固定结构，无随机源，离线可复现）。"""
from __future__ import annotations

import json
from pathlib import Path


def _small_manifest() -> dict:
    return {
        "file_id": "f-small",
        "dialect": "html",
        "sinks": [
            {"sink_id": "s1", "line": 10, "context_type": "html-text"},
            {"sink_id": "s2", "line": 20, "context_type": "html-js"},
            {"sink_id": "s3", "line": 30, "context_type": "sql-literal"},
            {"sink_id": "s4", "line": 40, "context_type": "shell-arg"},
        ],
        "paths": [
            # 安全：单族正确编码
            {"path_id": "p-safe-1", "source_id": "src1", "sink_id": "s1",
             "transforms": ["html_escape"]},
            # 连续同类合并：两次 html 编码合并仍安全
            {"path_id": "p-safe-merge", "source_id": "src1", "sink_id": "s1",
             "transforms": ["html_escape", "htmlspecialchars"]},
            # 抵消：html 编码后解码，净链为空 -> 顺序颠倒 E_ORDER
            {"path_id": "p-order-1", "source_id": "src2", "sink_id": "s1",
             "transforms": ["html_escape", "html_unescape"]},
            # 多族顺序颠倒：html+js 编码后 html 被解码
            {"path_id": "p-order-2", "source_id": "src2", "sink_id": "s2",
             "transforms": ["html_escape", "js_string_escape", "html_unescape"]},
            # 缺少编码：无任何变换 -> E_MISSING_ENC
            {"path_id": "p-missing-1", "source_id": "src3", "sink_id": "s3",
             "transforms": []},
            # 族不匹配：sql 汇点却做了 html 编码 -> E_MISMATCH
            {"path_id": "p-mismatch-1", "source_id": "src3", "sink_id": "s3",
             "transforms": ["html_escape"]},
            # 多族缺第二族：只要 html 不要 js -> E_MISSING_ENC
            {"path_id": "p-missing-2", "source_id": "src4", "sink_id": "s2",
             "transforms": ["html_escape"]},
            # 多族顺序颠倒：js 先 html 后，顺序错误但两族都在 -> E_MISMATCH
            {"path_id": "p-mismatch-2", "source_id": "src4", "sink_id": "s2",
             "transforms": ["js_string_escape", "html_escape"]},
            # 多族正确顺序 -> 安全
            {"path_id": "p-safe-2", "source_id": "src5", "sink_id": "s2",
             "transforms": ["html_escape", "js_string_escape"]},
            # 汇点聚合：s4 一条安全一条有缺陷
            {"path_id": "p-shell-safe", "source_id": "src6", "sink_id": "s4",
             "transforms": ["escapeshellarg"]},
            {"path_id": "p-shell-bad", "source_id": "src6", "sink_id": "s4",
             "transforms": ["shell_quote", "shell_unescape"]},
            # 重复路径：首次为安全真值，重复副本仍计数
            {"path_id": "p-dup", "source_id": "src7", "sink_id": "s1",
             "transforms": ["html_escape"]},
            {"path_id": "p-dup", "source_id": "src7", "sink_id": "s1",
             "transforms": ["html_escape"]},
        ],
    }


def _invalid_manifest() -> dict:
    return {
        "file_id": "f-invalid",
        "dialect": "cpp",  # E_BAD_DIALECT
        "sinks": [
            {"sink_id": "ok-sink", "line": 5, "context_type": "html-text"},
            {"sink_id": "bad-line", "line": -2, "context_type": "html-text"},  # E_BAD_LINE
            {"sink_id": "ok-sink", "line": 6, "context_type": "html-text"},  # E_DUP_SINK_ID
        ],
        "paths": [
            {"path_id": "ok-path", "source_id": "a", "sink_id": "ok-sink",
             "transforms": ["html_escape"]},
            {"path_id": "unknown-tf", "source_id": "a", "sink_id": "ok-sink",
             "transforms": ["totally_unknown_escape"]},  # E_UNKNOWN_TRANSFORM
            {"path_id": "ghost-ref", "source_id": "a", "sink_id": "missing-sink",
             "transforms": []},  # E_UNKNOWN_SINK_REF
            {"path_id": "ok-path", "source_id": "a", "sink_id": "ok-sink",
             "transforms": ["html_escape"]},  # E_DUP_PATH_ID
        ],
    }


def _large_manifest(path_total: int = 600, sink_total: int = 60) -> dict:
    """大清单：超过默认阈值以进入批处理模式，且大量路径共享等价类。"""
    sinks = [
        {"sink_id": f"ls{i}", "line": 100 + i, "context_type": "html-text"}
        for i in range(sink_total)
    ]
    chains = [
        ["html_escape"],
        ["html_escape", "htmlspecialchars"],
        ["html_escape", "html_unescape"],
        [],
        ["sql_quote"],
    ]
    paths = []
    for i in range(path_total):
        sink_index = i % sink_total
        paths.append({
            "path_id": f"lp{i}",
            "source_id": f"src{i % 7}",
            "sink_id": f"ls{sink_index}",
            "transforms": list(chains[i % len(chains)]),
        })
    return {"file_id": "f-large", "dialect": "html", "sinks": sinks, "paths": paths}


def generate_all(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (
        ("manifest_small.json", _small_manifest()),
        ("manifest_invalid.json", _invalid_manifest()),
        ("manifest_large.json", _large_manifest()),
    ):
        target = out / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written
