"""阶段三：实体槽位抽取与歧义消解。

对归一化文本做品牌/类目/属性三类词典最大匹配；
重叠候选按 最长优先 -> 词典优先级升序 -> 起始位置升序 -> 词条字典序 消解；
被吞并的候选记录为被抑制项，并输出 E_SLOT_OVERLAP 计数。
"""
from __future__ import annotations

from .errors import E_SLOT_OVERLAP
from .hashing import fingerprint

STAGE = "slots"


def extract_slots(text: str, dictionaries: list[dict]) -> dict:
    candidates: list[dict] = []
    for dic in dictionaries:
        for entry in dic["entries"]:
            start = 0
            while True:
                pos = text.find(entry, start)
                if pos < 0:
                    break
                candidates.append({
                    "type": dic["type"],
                    "entry": entry,
                    "start": pos,
                    "end": pos + len(entry),
                    "priority": int(dic["priority"]),
                })
                start = pos + 1

    # 消解排序：最长优先 -> 优先级升序 -> 起始位置升序 -> 词条字典序（保证全序）
    candidates.sort(key=lambda c: (-(c["end"] - c["start"]), c["priority"], c["start"], c["entry"]))

    accepted: list[dict] = []
    suppressed: list[dict] = []
    for cand in candidates:
        overlapped_by = None
        for acc in accepted:
            if cand["start"] < acc["end"] and acc["start"] < cand["end"]:
                overlapped_by = acc
                break
        if overlapped_by is None:
            accepted.append(cand)
        else:
            suppressed.append({**cand, "suppressed_by": overlapped_by["entry"]})

    accepted.sort(key=lambda c: (c["start"], c["end"], c["entry"]))
    suppressed.sort(key=lambda c: (c["start"], c["end"], c["entry"]))

    codes = [E_SLOT_OVERLAP] if suppressed else []
    result = {
        "slots": accepted,
        "suppressed": suppressed,
        "overlap_count": len(suppressed),
        "codes": codes,
    }
    result["fingerprint"] = fingerprint(result)
    return result
