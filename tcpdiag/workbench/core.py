"""会话核心：把 TCP 状态机、拥塞控制与 RTT 估计绑定推进。

与传输层解耦：发送报文进入 outbox，由 client.py 负责上线；
收到的对端报文由 client.py 分发到 on_* 方法。

观测与真值：
- 每个事件带单调时钟毫秒时间戳（时钟基准见 clock.py，快照中声明 clock_base）。
- 每次外部刺激（open/send/ack/timeout/close/fin...）记录到 inputs，
  处理后追加一个 checkpoint（状态、窗口、阈值、RTT 估计量、序号空间），
  供快照持久化与恢复后重放校验逐步比对。
"""

from .clock import MonotonicClock
from .congestion import CongestionController
from .config import DEFAULT_CONFIG, SNAPSHOT_VERSION
from .errors import E_SEQ, E_STATE, BizError
from .rtt import RTTEstimator
from .statemachine import TCPStateMachine

CHECKPOINT_FIELDS = (
    "tcp_state", "cwnd", "ssthresh", "srtt_q8", "rttvar_q4", "rto_ms",
    "send_una", "next_seq", "dup_ack_count", "phase",
)


class SessionCore:
    def __init__(self, config=None, clock=None, script_name=""):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.initial_config = dict(self.config)
        self.clock = clock if clock is not None else MonotonicClock()
        self.script_name = script_name
        self.sm = TCPStateMachine()
        self._rebuild_engines()
        self.send_una = 1          # 数据序号从 1 开始（SYN 占序号 0）
        self.next_seq = 1
        self.fin_seq = None
        self.syn_ts = None
        self.segments = {}         # seq -> {seq, len, send_ts, retrans}
        self.events = []
        self.inputs = []
        self.checkpoints = []
        self.outbox = []

    def _rebuild_engines(self):
        self.cc = CongestionController(self.config, log=self.log)
        self.rtt = RTTEstimator(self.config)

    # ---- 轨迹 ----
    def log(self, kind, **fields):
        entry = {"t": self.clock.now_ms(), "kind": kind}
        entry.update(fields)
        self.events.append(entry)
        return entry

    def checkpoint(self):
        cp = {
            "input_index": len(self.inputs) - 1,
            "tcp_state": self.sm.state,
            "cwnd": self.cc.cwnd,
            "ssthresh": self.cc.ssthresh,
            "srtt_q8": self.rtt.srtt_q8,
            "rttvar_q4": self.rtt.rttvar_q4,
            "rto_ms": self.rtt.rto_ms,
            "send_una": self.send_una,
            "next_seq": self.next_seq,
            "dup_ack_count": self.cc.dup_ack_count,
            "phase": self.cc.phase,
        }
        self.checkpoints.append(cp)
        return cp

    # ---- 连接生命周期 ----
    def open(self):
        self.inputs.append({"kind": "open", "ts": self.clock.now_ms()})
        old, new = self.sm.transition("SYN", seq=0, now=self.clock.now_ms())
        self.syn_ts = self.clock.now_ms()
        self.outbox.append({"type": "SYN", "seq": 0})
        self.log("state", old=old, new=new, trigger="SYN", seq=0)
        self.log("send", packet="SYN", seq=0)
        self.checkpoint()

    def on_syn_ack(self, delay_ms=0):
        arrival = self.clock.note_arrival(self.syn_ts, delay_ms)
        self.inputs.append({"kind": "syn_ack", "ts": arrival, "delay_ms": delay_ms})
        old, new = self.sm.transition("SYN_ACK", seq=0, now=arrival)
        self.log("state", old=old, new=new, trigger="SYN_ACK", seq=0)
        self.log("ack", packet="SYN_ACK", ack=1)
        self._rtt_sample(self.syn_ts, arrival)
        self.outbox.append({"type": "ACK", "ack": 1})
        self.checkpoint()

    def close(self):
        self.inputs.append({"kind": "close", "ts": self.clock.now_ms()})
        self.fin_seq = self.next_seq
        old, new = self.sm.transition("FIN", seq=self.fin_seq,
                                      now=self.clock.now_ms())
        self.outbox.append({"type": "FIN", "seq": self.fin_seq})
        self.log("state", old=old, new=new, trigger="FIN", seq=self.fin_seq)
        self.log("send", packet="FIN", seq=self.fin_seq)
        self.checkpoint()

    def on_fin(self, delay_ms=0):
        arrival = self.clock.note_arrival(self.clock.now_ms(), delay_ms)
        self.inputs.append({"kind": "fin", "ts": arrival, "delay_ms": delay_ms})
        old, new = self.sm.transition("RCV_FIN", seq=self.fin_seq, now=arrival)
        self.log("state", old=old, new=new, trigger="RCV_FIN", seq=self.fin_seq)
        self.checkpoint()

    def on_fin_ack(self, delay_ms=0):
        arrival = self.clock.note_arrival(self.clock.now_ms(), delay_ms)
        self.inputs.append({"kind": "fin_ack", "ts": arrival, "delay_ms": delay_ms})
        old, new = self.sm.transition("FIN_ACK", seq=self.fin_seq, now=arrival)
        self.log("state", old=old, new=new, trigger="FIN_ACK", seq=self.fin_seq)
        self.checkpoint()

    # ---- 数据发送 ----
    def send_data(self, total_len):
        self.inputs.append({"kind": "send", "len": total_len,
                            "ts": self.clock.now_ms()})
        if self.sm.state != "ESTABLISHED":
            raise BizError(E_STATE,
                           f"cannot send data in state {self.sm.state}")
        sent = 0
        while sent < total_len:
            in_flight = self.next_seq - self.send_una
            allowance = self.cc.cwnd - in_flight
            if allowance <= 0:
                break
            length = min(self.config["mss"], total_len - sent, allowance)
            self._send_segment(self.next_seq, length)
            sent += length
        self.checkpoint()
        return total_len - sent

    def _send_segment(self, seq, length):
        seg = {"seq": seq, "len": length,
               "send_ts": self.clock.now_ms(), "retrans": 0}
        self.segments[seq] = seg
        self.next_seq = seq + length
        self.outbox.append({"type": "DATA", "seq": seq, "len": length,
                            "retrans": 0})
        self.log("send", packet="DATA", seq=seq, len=length, retrans=0)

    def _retransmit(self, seg):
        seg["send_ts"] = self.clock.now_ms()
        seg["retrans"] += 1
        self.outbox.append({"type": "DATA", "seq": seg["seq"],
                            "len": seg["len"], "retrans": seg["retrans"]})
        self.log("send", packet="DATA", seq=seg["seq"], len=seg["len"],
                 retrans=seg["retrans"])

    # ---- 确认处理 ----
    def on_ack(self, ack, dup=False, delay_ms=0):
        ref = self.segments.get(self.send_una)
        base = ref["send_ts"] if ref is not None else self.clock.now_ms()
        arrival = self.clock.note_arrival(base, delay_ms)
        self.inputs.append({"kind": "ack", "ack": ack, "dup": bool(dup),
                            "ts": arrival})
        if ack < 0 or ack > self.next_seq:
            self.log("error", code=E_SEQ, ack=ack, next_seq=self.next_seq)
            raise BizError(E_SEQ,
                           f"ack {ack} out of range (next_seq={self.next_seq})")
        if ack > self.send_una:
            self._on_cumulative_ack(ack, arrival)
        elif ack == self.send_una or dup:
            self.log("ack", ack=ack, bytes_acked=0, dup=True)
            if self.cc.on_dup_ack() and self.segments:
                self._retransmit(self.segments[min(self.segments)])
        else:
            # ack < send_una：乱序到达的过期确认，不推进确认点也不计重复确认
            self.log("stale_ack", ack=ack, send_una=self.send_una)
        self.checkpoint()

    def _on_cumulative_ack(self, ack, arrival):
        newly = [self.segments[s] for s in sorted(self.segments)
                 if s >= self.send_una
                 and s + self.segments[s]["len"] <= ack]
        bytes_acked = sum(s["len"] for s in newly)
        sample_seg = None
        for seg in newly:
            if seg["retrans"] == 0:
                sample_seg = seg
            del self.segments[seg["seq"]]
        self.send_una = ack
        self.log("ack", ack=ack, bytes_acked=bytes_acked, dup=False)
        self.cc.on_new_ack(bytes_acked)
        if sample_seg is not None:
            self._rtt_sample(sample_seg["send_ts"], arrival)
        else:
            # Karn 规则：新确认的段全部为重传段，排除本次样本
            self.log("karn_excluded", ack=ack)

    def _rtt_sample(self, send_ts, arrival_ts):
        sample = arrival_ts - send_ts
        if sample <= 0:
            # 显式规则：时钟回拨 / 非正样本，丢弃并记录，不静默采用
            self.log("rtt_rejected", reason="non_positive_sample",
                     sample=sample)
            return
        self.rtt.add_sample(sample)
        self.log("rtt", sample=sample, srtt_ms=self.rtt.srtt_ms,
                 rttvar_ms=self.rtt.rttvar_ms, rto_ms=self.rtt.rto_ms)

    # ---- 超时与重传 ----
    def tick(self):
        """由定时器驱动：最早已发送未确认段超过 RTO 则触发超时重传。"""
        if not self.segments:
            return False
        oldest = self.segments[min(self.segments)]
        if self.clock.now_ms() - oldest["send_ts"] >= self.rtt.rto_ms:
            return self.on_timeout()
        return False

    def on_timeout(self):
        self.inputs.append({"kind": "timeout", "ts": self.clock.now_ms()})
        if not self.segments:
            self.checkpoint()
            return False
        self.cc.on_timeout()
        self.log("timeout", cwnd=self.cc.cwnd, ssthresh=self.cc.ssthresh,
                 rto_ms=self.rtt.rto_ms)
        self._retransmit(self.segments[min(self.segments)])
        self.checkpoint()
        return True

    # ---- 快照 ----
    def snapshot(self):
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "script_name": self.script_name,
            "clock_base": self.clock.base_name,
            "initial_config": self.initial_config,
            "session_config": self.config,
            "state": {
                "tcp_state": self.sm.state,
                "cwnd": self.cc.cwnd,
                "ssthresh": self.cc.ssthresh,
                "dup_ack_count": self.cc.dup_ack_count,
                "srtt_q8": self.rtt.srtt_q8,
                "rttvar_q4": self.rtt.rttvar_q4,
                "rto_ms": self.rtt.rto_ms,
                "send_una": self.send_una,
                "next_seq": self.next_seq,
                "fin_seq": self.fin_seq,
                "syn_ts": self.syn_ts,
                "clock_now_ms": self.clock.now_ms(),
            },
            "segments": [dict(s) for s in self.segments.values()],
            "events": list(self.events),
            "inputs": list(self.inputs),
            "checkpoints": list(self.checkpoints),
        }
