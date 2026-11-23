"""确定性样本生成：多轮会话清单（JSONL）只依赖固定整数种子与受控词表。"""

import json
import random
from pathlib import Path

from .errors import E_FORMAT_CORRUPT, MemError
from .vocab import controlled_vocab

TURN_FIELDS = ("session_id", "turn_no", "speaker", "text_tokens", "token_len", "timestamp_ms", "revision")


def generate_sessions(seed=20240101, num_sessions=3, min_turns=8, max_turns=12):
    """按固定种子从受控词表拼装 token 序列，返回 {session_id: session}。"""
    vocab = controlled_vocab()
    rng = random.Random(seed)
    base_ts = 1_700_000_000_000
    sessions = {}
    for s in range(num_sessions):
        session_id = f"s{s + 1:02d}"
        turn_count = rng.randint(min_turns, max_turns)
        turns = []
        for t in range(1, turn_count + 1):
            token_len = rng.randint(6, 14)
            tokens = [vocab[rng.randrange(len(vocab))] for _ in range(token_len)]
            turns.append(
                {
                    "session_id": session_id,
                    "turn_no": t,
                    "speaker": "user" if t % 2 == 1 else "agent",
                    "text_tokens": tokens,
                    "token_len": token_len,
                    "timestamp_ms": base_ts + s * 10_000_000 + t * 60_000,
                    "revision": 1,
                }
            )
        sessions[session_id] = {"session_id": session_id, "turns": turns}
    return sessions


def write_sessions_jsonl(sessions, path):
    with open(path, "w", encoding="utf-8") as fh:
        for session_id in sorted(sessions):
            for turn in sessions[session_id]["turns"]:
                fh.write(json.dumps(turn, ensure_ascii=True, sort_keys=True) + "\n")


def load_sessions_jsonl(path):
    """会话摄入与校验：必填字段、token_len 与 text_tokens 长度一致。"""
    sessions = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MemError(E_FORMAT_CORRUPT, f"会话行 JSON 不可解析: {exc}", line_no=line_no)
            missing = [f for f in TURN_FIELDS if f not in doc]
            session_id = doc.get("session_id")
            if missing:
                raise MemError(
                    E_FORMAT_CORRUPT, f"会话行必填字段缺失: {missing}",
                    session_id=session_id, line_no=line_no,
                )
            if doc["token_len"] != len(doc["text_tokens"]):
                raise MemError(
                    E_FORMAT_CORRUPT,
                    f"token_len({doc['token_len']}) 与 text_tokens 长度({len(doc['text_tokens'])}) 不一致",
                    session_id=session_id, line_no=line_no,
                )
            session = sessions.setdefault(session_id, {"session_id": session_id, "turns": []})
            session["turns"].append(doc)
    for session in sessions.values():
        session["turns"].sort(key=lambda t: t["turn_no"])
    return sessions


def append_turn(session, text_tokens, timestamp_ms):
    """构造追加一轮后的新会话对象（revision 递增）。"""
    turns = [dict(t) for t in session["turns"]]
    last = turns[-1]
    turns.append(
        {
            "session_id": session["session_id"],
            "turn_no": last["turn_no"] + 1,
            "speaker": "user" if (last["turn_no"] + 1) % 2 == 1 else "agent",
            "text_tokens": list(text_tokens),
            "token_len": len(text_tokens),
            "timestamp_ms": timestamp_ms,
            "revision": 1,
        }
    )
    return {"session_id": session["session_id"], "turns": turns}


def revise_turn(session, turn_no, new_tokens):
    """构造修订某轮文本后的新会话对象（该轮 revision 递增）。"""
    turns = []
    for t in session["turns"]:
        t = dict(t)
        if t["turn_no"] == turn_no:
            t["text_tokens"] = list(new_tokens)
            t["token_len"] = len(new_tokens)
            t["revision"] = t["revision"] + 1
        turns.append(t)
    return {"session_id": session["session_id"], "turns": turns}


def generate_entry_samples(sessions, spec, directory):
    """由确定性流水线生成三种格式并存、互相一致的记忆条目样本文件。"""
    from .formats import write_entries
    from .pipeline import run_pipeline
    from .scoring import score_entries

    result = run_pipeline(sessions, spec)
    scores = score_entries(result.entries, sessions)
    entries = [
        e.with_fields(importance=scores[e.entry_id]["score"]) for e in result.entries
    ]
    paths = {}
    for version in (1, 2, 3):
        path = Path(directory) / f"memory_entries_v{version}.txt"
        write_entries(entries, path, version)
        paths[version] = str(path)
    return paths, entries
