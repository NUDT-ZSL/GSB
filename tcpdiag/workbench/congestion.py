"""拥塞窗口与慢启动阈值推进（全部整数运算，单位：字节）。

规则：
- 慢启动（cwnd < ssthresh）：每确认 1 字节，cwnd += 1。
- 拥塞避免（cwnd >= ssthresh）：cwnd += max(1, MSS*acked/cwnd)，
  即每个往返窗口约增 1 个 MSS 的整数近似。
- 重复确认达到阈值（默认 3）：快速重传，ssthresh = max(cwnd/2, 2*MSS)，cwnd = ssthresh。
- 超时：回慢启动，ssthresh = max(cwnd/2, 2*MSS)，cwnd = 1*MSS。
所有变化通过 log 回调写入事件轨迹。
"""


class CongestionController:
    def __init__(self, config, log=None):
        self.mss = config["mss"]
        self.threshold = config["dup_ack_threshold"]
        self.cwnd = config["init_cwnd_mss"] * self.mss
        self.ssthresh = config["init_ssthresh"]
        self.dup_ack_count = 0
        self.log = log if log is not None else (lambda **kw: None)

    @property
    def phase(self):
        return "slow_start" if self.cwnd < self.ssthresh else "congestion_avoidance"

    def on_new_ack(self, bytes_acked):
        self.dup_ack_count = 0
        if self.cwnd < self.ssthresh:
            self.cwnd += bytes_acked
            reason = "slow_start"
        else:
            self.cwnd += max(1, (self.mss * bytes_acked) // self.cwnd)
            reason = "congestion_avoidance"
        self.log(kind="cwnd", reason=reason, cwnd=self.cwnd,
                 ssthresh=self.ssthresh, acked=bytes_acked)

    def on_dup_ack(self):
        self.dup_ack_count += 1
        self.log(kind="dup_ack", count=self.dup_ack_count)
        if self.dup_ack_count == self.threshold:
            self.ssthresh = max(self.cwnd // 2, 2 * self.mss)
            self.cwnd = self.ssthresh
            self.log(kind="fast_retransmit", cwnd=self.cwnd, ssthresh=self.ssthresh)
            return True
        return False

    def on_timeout(self):
        self.dup_ack_count = 0
        self.ssthresh = max(self.cwnd // 2, 2 * self.mss)
        self.cwnd = self.mss
        self.log(kind="cwnd", reason="timeout", cwnd=self.cwnd,
                 ssthresh=self.ssthresh)
