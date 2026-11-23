"""Deterministic local sample generation and editable session helpers."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .canonical import atomic_write_json, canonical_bytes, jsonl_bytes
from .models import Session, SummaryUnit, Turn, session_from_dict


DEFAULT_SEED = 20261124


VOCAB_V1: Tuple[str, ...] = (
    "你好",
    "您好",
    "您",
    "我",
    "的",
    "是",
    "了",
    "吗",
    "请",
    "来",
    "帮您",
    "查询",
    "进度",
    "提供",
    "核对",
    "账户",
    "无法",
    "登录",
    "密码",
    "重置",
    "链接",
    "没有",
    "收到",
    "发送",
    "检查",
    "请问",
    "手机号",
    "短信",
    "如果",
    "仍然",
    "失败",
    "可以",
    "联系",
    "客服",
    "退款",
    "申请",
    "已经",
    "提交",
    "订单号",
    "什么",
    "时候",
    "到账",
    "三个",
    "工作日",
    "还",
    "系统",
    "显示",
    "正在",
    "处理",
    "预计",
    "今天",
    "注意",
    "查收",
    "物流",
    "派送中",
    "但是",
    "地址",
    "需要",
    "修改",
    "和",
    "登记",
    "稍等",
    "会",
    "尽快",
    "回复",
    "优惠券",
    "过期",
    "补发",
    "规则",
    "核验",
    "补偿",
    "耐心",
    "等待",
    "问题",
    "还没",
    "解决",
    "加急",
    "回访",
    "已",
    "为",
    "大概",
    "安全",
    "工单",
    "编号",
    "处理完成",
    "感谢",
    "1001",
    "1002",
    "13800000000",
    "user100",
)


VOCAB_V2_ADDITIONS: Tuple[str, ...] = (
    "补偿单",
    "加急单",
    "回访单",
    "核验码",
    "安全中心",
)


THEMES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "refund",
        "user": (
            ("你好", "我", "的", "退款", "申请", "已经", "提交"),
            ("订单号", "是", "1001", "请问", "什么", "时候", "到账"),
            ("已经", "三个", "工作日", "了", "还", "没有", "到账"),
        ),
        "agent": (
            ("您好", "我", "来", "帮您", "查询", "退款", "进度"),
            ("请", "稍等", "系统", "显示", "退款", "正在", "处理"),
            ("预计", "今天", "到账", "请", "注意", "查收"),
        ),
        "forbidden": ("大概",),
        "preserve": ("退款", "到账"),
    },
    {
        "name": "login",
        "user": (
            ("你好", "我", "的", "账户", "无法", "登录"),
            ("密码", "重置", "链接", "没有", "收到"),
            ("手机号", "是", "13800000000"),
        ),
        "agent": (
            ("您好", "请", "提供", "手机号", "我", "来", "核对"),
            ("链接", "已经", "发送", "请", "检查", "短信"),
            ("如果", "仍然", "失败", "可以", "联系", "客服"),
        ),
        "forbidden": ("大概",),
        "preserve": ("账户", "链接"),
    },
    {
        "name": "logistics",
        "user": (
            ("你好", "物流", "显示", "派送中", "但是", "没有", "收到"),
            ("订单号", "是", "1002"),
            ("地址", "需要", "修改", "可以", "吗"),
        ),
        "agent": (
            ("您好", "我", "来", "帮您", "查询", "物流"),
            ("请", "提供", "订单号", "和", "地址"),
            ("已经", "登记", "稍等", "客服", "会", "尽快", "回复"),
        ),
        "forbidden": ("大概",),
        "preserve": ("物流", "地址"),
    },
    {
        "name": "coupon",
        "user": (
            ("你好", "优惠券", "过期", "了", "可以", "补发", "吗"),
            ("我", "的", "账户", "是", "user100"),
            ("问题", "还没", "解决"),
        ),
        "agent": (
            ("您好", "优惠券", "规则", "需要", "核验"),
            ("我", "来", "帮您", "申请", "补发"),
            ("补偿", "已经", "提交", "请", "耐心", "等待"),
        ),
        "forbidden": ("大概",),
        "preserve": ("优惠券", "补发"),
    },
)


def _units_for_turn(
    session_id: str, turn_index: int, words: Sequence[str]
) -> List[Dict[str, Any]]:
    """Build one or two truth units while preserving deterministic ordering."""

    if len(words) >= 6 and turn_index % 3 == 2:
        split = 3
        parts = (tuple(words[:split]), tuple(words[split:]))
    else:
        parts = (tuple(words),)
    units = []
    for unit_index, part in enumerate(parts):
        units.append(
            {
                "unit_id": f"{session_id}-T{turn_index:03d}-U{unit_index:02d}",
                "turn_index": turn_index,
                "unit_index": unit_index,
                "text": " ".join(part),
                "truth": True,
            }
        )
    return units


def build_session(
    ordinal: int,
    theme: Dict[str, Any],
    *,
    seed: int = DEFAULT_SEED,
    session_id: str | None = None,
) -> Dict[str, Any]:
    sid = session_id or f"S{ordinal + 1:03d}"
    rng = random.Random(seed + ordinal * 7919 + sum(ord(term) for term in theme["name"]))
    turns: List[Dict[str, Any]] = []
    units: List[Dict[str, Any]] = []
    for turn_index in range(6):
        is_user = turn_index % 2 == 0
        variants = theme["user"] if is_user else theme["agent"]
        words = list(variants[(turn_index // 2) % len(variants)])
        if turn_index == 4 and is_user and rng.randrange(2) == 0:
            words.append("请")
            words.append("尽快")
            words.append("回复")
        if turn_index == 5 and not is_user and rng.randrange(2) == 0:
            words.append("感谢")
            words.append("耐心")
            words.append("等待")
        turns.append(
            {
                "turn_index": turn_index,
                "role": "user" if is_user else "agent",
                "text": " ".join(words) + "。",
            }
        )
        units.extend(_units_for_turn(sid, turn_index, words))
    return {
        "session_id": sid,
        "content_version": 1,
        "turns": turns,
        "units": units,
        "rewrite_constraints": {
            "forbidden_words": list(theme["forbidden"]),
            "preserve_terms": list(theme["preserve"]),
            "max_chars": 80,
        },
    }


def generate_session_records(
    count: int = 4, *, seed: int = DEFAULT_SEED
) -> List[Dict[str, Any]]:
    shuffled = list(THEMES)
    random.Random(seed).shuffle(shuffled)
    return [
        build_session(index, shuffled[index % len(shuffled)], seed=seed)
        for index in range(count)
    ]


def append_turns_to_record(
    record: Dict[str, Any], *, seed: int = DEFAULT_SEED
) -> Dict[str, Any]:
    """Append two deterministic turns and matching truth units."""

    sid = str(record["session_id"])
    current_turns = list(record["turns"])
    next_index = max(turn["turn_index"] for turn in current_turns) + 1
    digest = sum(ord(char) for char in sid)
    rng = random.Random(seed + digest + next_index)
    user_words = ["请", "加急", "处理", "问题"]
    agent_words = ["已", "为", "您", "登记", "加急", "回访"]
    if rng.randrange(2) == 0:
        user_words.extend(["感谢", "客服"])
    extra_turns = [
        {"turn_index": next_index, "role": "user", "text": " ".join(user_words) + "。"},
        {
            "turn_index": next_index + 1,
            "role": "agent",
            "text": " ".join(agent_words) + "。",
        },
    ]
    extra_units: List[Dict[str, Any]] = []
    extra_units.extend(_units_for_turn(sid, next_index, user_words))
    extra_units.extend(_units_for_turn(sid, next_index + 1, agent_words))
    result = json.loads(canonical_bytes(record).decode("utf-8"))
    result["content_version"] = int(record.get("content_version", 1)) + 1
    result["turns"].extend(extra_turns)
    result["units"].extend(extra_units)
    return result


def vocabulary_v1() -> Dict[str, Any]:
    return {"vocab_version": "v1", "words": list(VOCAB_V1)}


def vocabulary_v2() -> Dict[str, Any]:
    return {
        "vocab_version": "v2",
        "words": list(VOCAB_V1 + VOCAB_V2_ADDITIONS),
    }


def params_v1() -> Dict[str, Any]:
    return {
        "config_version": 1,
        "precision": "fp32",
        "block_size": 2,
        "weights": {
            "position": 0.30,
            "length": 0.20,
            "frequency": 0.30,
            "coverage": 0.20,
        },
        "rewrite_strength": 0.35,
        "smoothing": 0.50,
        "limits": {
            "max_sessions_per_stage": 100,
            "max_resident_frequency": 10000,
            "max_intermediate_memory_bytes": 8 * 1024 * 1024,
        },
        "resource_policy": "error",
    }


def params_v2() -> Dict[str, Any]:
    value = params_v1()
    value["config_version"] = 2
    value["rewrite_strength"] = 0.65
    value["smoothing"] = 0.75
    return value


def write_dataset(root: Path, *, force: bool = False, seed: int = DEFAULT_SEED) -> None:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    targets = [
        data_dir / "sessions.jsonl",
        data_dir / "vocabulary_v1.json",
        data_dir / "vocabulary_v2.json",
        data_dir / "params_v1.json",
        data_dir / "params_v2.json",
    ]
    if not force and all(path.exists() for path in targets):
        return
    records = generate_session_records(seed=seed)
    (data_dir / "sessions.jsonl").write_bytes(jsonl_bytes(records))
    atomic_write_json(data_dir / "vocabulary_v1.json", vocabulary_v1())
    atomic_write_json(data_dir / "vocabulary_v2.json", vocabulary_v2())
    atomic_write_json(data_dir / "params_v1.json", params_v1())
    atomic_write_json(data_dir / "params_v2.json", params_v2())
    checkpoint_dir = root / "checkpoints" / "demo"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    notes = checkpoint_dir / "README.txt"
    if not notes.exists():
        notes.write_text(
            "Demo checkpoint directory. Stage snapshots appear here after runs.\n",
            encoding="utf-8",
        )


def add_session_record(
    path: Path, session_id: str, *, seed: int = DEFAULT_SEED
) -> Dict[str, Any]:
    records = _read_records(path)
    if any(record["session_id"] == session_id for record in records):
        from .errors import BusinessError

        raise BusinessError(
            "E_DUPLICATE_SESSION",
            f"session {session_id!r} already exists",
            session_id=session_id,
            field="session_id",
        )
    theme = THEMES[sum(ord(char) for char in session_id) % len(THEMES)]
    record = build_session(
        len(records),
        theme,
        seed=seed,
        session_id=session_id,
    )
    records.append(record)
    records.sort(key=lambda item: item["session_id"])
    path.write_bytes(jsonl_bytes(records))
    return record


def remove_session_record(path: Path, session_id: str) -> Dict[str, Any]:
    records = _read_records(path)
    kept = [record for record in records if record["session_id"] != session_id]
    if len(kept) == len(records):
        from .errors import BusinessError

        raise BusinessError(
            "E_SESSION_NOT_FOUND",
            f"session {session_id!r} does not exist",
            session_id=session_id,
            field="session_id",
        )
    path.write_bytes(jsonl_bytes(kept))
    return {"removed": session_id, "remaining": len(kept)}


def _read_records(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records
