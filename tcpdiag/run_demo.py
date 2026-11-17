#!/usr/bin/env python3
"""确定性演示：顺序执行三个场景并输出稳定总结。

场景：
  normal   正常三次握手 + 慢启动/拥塞避免窗口推进 + 正常关闭
  loss     丢包注入 -> 3 次重复确认 -> 快速重传；再丢包 -> RTO 超时回慢启动
  snapshot 会话快照持久化 -> 恢复后继续交互 -> 重放校验（含篡改反例 E_REPLAY）

所有通信均在 127.0.0.1 高位端口；客户端使用确定性虚拟时钟，
对端 delay_ms 为虚拟延迟，输出与真实运行时长无关。
"""

import copy
import json
from pathlib import Path

from workbench.client import SessionClient
from workbench.clock import DeterministicClock
from workbench.core import SessionCore
from workbench.peer import ScriptedPeer
from workbench.replay import verify_replay
from workbench.snapshot import (load_snapshot, restore_into,
                                restore_session, save_snapshot)

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
SNAP_DIR = ROOT / "snapshots"


def start_peer(script_name):
    script = json.loads((SCRIPTS / script_name).read_text(encoding="utf-8"))
    peer = ScriptedPeer(script)
    peer.start()
    peer.ready.wait(2.0)
    return peer


def summary(name, core, extra=""):
    return (f"[scenario={name}] state={core.sm.state} "
            f"cwnd={core.cc.cwnd} ssthresh={core.cc.ssthresh} "
            f"srtt_ms={core.rtt.srtt_ms} rto_ms={core.rtt.rto_ms} "
            f"send_una={core.send_una} "
            f"unacked={core.next_seq - core.send_una} "
            f"events={len(core.events)} "
            f"checkpoints={len(core.checkpoints)} {extra}").rstrip()


def scenario_normal():
    core = SessionCore(config={"init_cwnd_mss": 2, "init_ssthresh": 4000},
                       clock=DeterministicClock(), script_name="normal")
    peer = start_peer("peer_normal.json")
    client = SessionClient(core, port=peer.port)
    client.connect()
    client.pump()
    remaining = 4000
    while remaining:
        remaining = client.send_data(remaining)
        client.pump()
    client.close()
    client.pump()
    client.shutdown()
    peer.stop()
    return core


def scenario_loss():
    core = SessionCore(config={"init_cwnd_mss": 4, "init_ssthresh": 16000},
                       clock=DeterministicClock(), script_name="loss_dupack")
    peer = start_peer("peer_loss_dupack.json")
    client = SessionClient(core, port=peer.port)
    client.connect()
    client.pump()
    client.send_data(4000)          # 首段被丢弃，后续段触发 3 次重复确认
    client.pump()                   # 快速重传 seq=1
    client.pump()                   # 累计确认 4001
    client.send_data(2000)          # 两段均被脚本丢弃
    client.pump()
    core.clock.advance(core.rtt.rto_ms + 1)   # 虚拟时钟越过 RTO
    client.tick()                   # 超时 -> 回慢启动，重传 4001
    client.pump()
    core.clock.advance(core.rtt.rto_ms + 1)
    client.tick()                   # 再次超时，重传 5001
    client.pump()
    client.close()
    client.pump()
    client.shutdown()
    peer.stop()
    fast = sum(1 for e in core.events if e["kind"] == "fast_retransmit")
    timeouts = sum(1 for e in core.events if e["kind"] == "timeout")
    karn = sum(1 for e in core.events if e["kind"] == "karn_excluded")
    return core, f"fast_retransmit={fast} timeouts={timeouts} karn_excluded={karn}"


def scenario_snapshot():
    core = SessionCore(config={"init_cwnd_mss": 4},
                       clock=DeterministicClock(), script_name="snapshot")
    peer = start_peer("peer_snapshot.json")
    client = SessionClient(core, port=peer.port)
    client.connect()
    client.pump()
    client.send_data(2000)
    client.pump()
    SNAP_DIR.mkdir(exist_ok=True)
    snap_path = SNAP_DIR / "session_snapshot.json"
    save_snapshot(core, str(snap_path))
    snap = load_snapshot(str(snap_path))

    replay_ok = verify_replay(snap)

    # 版本不一致 -> 整体替换（不增量合并）
    foreign = SessionCore(config={"config_version": 999},
                          clock=DeterministicClock())
    restore_into(foreign, snap)
    replaced = (foreign.config["config_version"]
                == snap["session_config"]["config_version"])

    # 同版本恢复，接管同一条连接继续推进
    restored = restore_session(snap, clock=DeterministicClock())
    client.core = restored
    client.send_data(2000)
    client.pump()
    client.close()
    client.pump()
    client.shutdown()
    peer.stop()

    # 篡改反例：重放校验必须报 E_REPLAY 并定位字段
    tampered = copy.deepcopy(snap)
    tampered["checkpoints"][1]["cwnd"] += 1
    replay_bad = verify_replay(tampered)

    extra = (f"replay={'OK' if replay_ok['ok'] else 'FAIL'}"
             f"(checked={replay_ok.get('checked')}) "
             f"version_replace={'OK' if replaced else 'FAIL'} "
             f"tamper={replay_bad.get('code')}"
             f"(index={replay_bad.get('input_index')},"
             f"field={replay_bad.get('field')})")
    return restored, extra


def main():
    core_a = scenario_normal()
    print(summary("normal", core_a, "result=OK"))
    core_b, extra_b = scenario_loss()
    print(summary("loss", core_b, f"{extra_b} result=OK"))
    core_c, extra_c = scenario_snapshot()
    print(summary("snapshot", core_c, f"{extra_c} result=OK"))
    print("DEMO COMPLETE")


if __name__ == "__main__":
    main()
