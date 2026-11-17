"""恢复后一致性重放校验。

从快照的 initial_config 与确定性时钟重建会话，按快照记录的 inputs
（含每条输入的单调时钟时间戳）逐步重放，每步比对 checkpoint 中的
状态、拥塞窗口、阈值与 RTT 估计量；首个不一致即返回 E_REPLAY，
并定位到输入序号、关联报文序号与字段名。
"""

from .clock import DeterministicClock
from .core import CHECKPOINT_FIELDS, SessionCore
from .errors import E_REPLAY


def _apply(core, clock, inp):
    clock.advance_to(inp.get("ts") or 0)
    kind = inp["kind"]
    if kind == "open":
        core.open()
    elif kind == "send":
        core.send_data(inp["len"])
    elif kind == "syn_ack":
        core.on_syn_ack(0)
    elif kind == "ack":
        core.on_ack(inp["ack"], inp.get("dup", False), 0)
    elif kind == "timeout":
        core.on_timeout()
    elif kind == "close":
        core.close()
    elif kind == "fin":
        core.on_fin(0)
    elif kind == "fin_ack":
        core.on_fin_ack(0)
    else:
        raise ValueError(f"unknown input kind {kind!r}")


def verify_replay(snap):
    clock = DeterministicClock()
    core = SessionCore(config=snap["initial_config"], clock=clock,
                       script_name=snap.get("script_name", ""))
    expected = snap["checkpoints"]
    for i, inp in enumerate(snap["inputs"]):
        _apply(core, clock, inp)
        if i >= len(expected):
            break
        exp = expected[i]
        act = core.checkpoints[-1]
        for field in CHECKPOINT_FIELDS:
            if exp.get(field) != act.get(field):
                return {
                    "ok": False,
                    "code": E_REPLAY,
                    "input_index": i,
                    "seq": inp.get("ack", inp.get("seq", inp.get("len"))),
                    "field": field,
                    "expected": exp.get(field),
                    "actual": act.get(field),
                }
    return {"ok": True, "checked": min(len(snap["inputs"]), len(expected))}
