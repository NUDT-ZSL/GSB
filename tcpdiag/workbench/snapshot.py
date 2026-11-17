"""会话快照：版本识别与整体替换恢复。

恢复策略：
- snapshot_version 与当前格式不一致 -> E_VERSION，拒绝恢复；
- 快照内 session_config.config_version 与当前会话配置版本不一致 ->
  整体替换（配置与全部状态一并采用快照内容，记录 config_replaced 事件），
  绝不做增量合并；
- 版本一致 -> 同样整体替换状态字段。
恢复后 inputs/checkpoints 轨迹延续，可继续推进新的报文交互。
"""

import json

from .clock import DeterministicClock
from .config import SNAPSHOT_VERSION
from .core import SessionCore
from .errors import E_VERSION, BizError


def save_snapshot(core, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(core.snapshot(), f, ensure_ascii=False, indent=2)


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def restore_into(core, snap):
    if snap.get("snapshot_version") != SNAPSHOT_VERSION:
        raise BizError(
            E_VERSION,
            f"snapshot_version {snap.get('snapshot_version')} "
            f"!= supported {SNAPSHOT_VERSION}")
    incoming = snap["session_config"]
    config_replaced = False
    if incoming.get("config_version") != core.config.get("config_version"):
        core.config.clear()
        core.config.update(incoming)
        core.initial_config = dict(snap["initial_config"])
        config_replaced = True
    core._rebuild_engines()
    st = snap["state"]
    core.sm.force(st["tcp_state"])
    core.cc.cwnd = st["cwnd"]
    core.cc.ssthresh = st["ssthresh"]
    core.cc.dup_ack_count = st["dup_ack_count"]
    core.rtt.srtt_q8 = st["srtt_q8"]
    core.rtt.rttvar_q4 = st["rttvar_q4"]
    core.send_una = st["send_una"]
    core.next_seq = st["next_seq"]
    core.fin_seq = st["fin_seq"]
    core.syn_ts = st["syn_ts"]
    core.segments = {int(s["seq"]): dict(s) for s in snap["segments"]}
    core.events = list(snap["events"])
    core.inputs = list(snap["inputs"])
    core.checkpoints = list(snap["checkpoints"])
    core.script_name = snap.get("script_name", "")
    core.clock.advance_to(st.get("clock_now_ms") or 0)
    if config_replaced:
        core.log("config_replaced",
                 new_config_version=incoming.get("config_version"))
    core.log("snapshot_restored", config_replaced=config_replaced)
    return core


def restore_session(snap, clock=None):
    clock = clock if clock is not None else DeterministicClock()
    core = SessionCore(config=snap["initial_config"], clock=clock,
                       script_name=snap.get("script_name", ""))
    return restore_into(core, snap)
