"""RTT 估计与 RTO 定时器（整数定点实现）。

公式（RFC 6298 风格，alpha=1/8，beta=1/4）：
    首样本:  SRTT = R,  RTTVAR = R/2
    后续:    RTTVAR = (3/4)*RTTVAR + (1/4)*|SRTT - R|
             SRTT   = (7/8)*SRTT   + (1/8)*R
    RTO    = SRTT + max(G, 4*RTTVAR)，并夹在 [min_rto, max_rto]

缩放因子：SRTT 以 q8（乘以 8）保存，RTTVAR 以 q4（乘以 4）保存，
全部运算为整数移位，无任何浮点，保证跨平台重放结果逐位一致。

显式规则：
- 样本不足（尚无任何有效样本）：RTO 取 config.initial_rto_ms。
- 时钟回拨 / 非正样本（R <= 0）：抛出 E_CLOCK，调用方记录 rtt_rejected 事件并丢弃。
- Karn 规则：被重传过的段的确认不产生样本（由 SessionCore 判定并记录 karn_excluded）。
"""

from .errors import E_CLOCK, BizError


class RTTEstimator:
    def __init__(self, config):
        self.granularity = config["granularity_ms"]
        self.min_rto = config["min_rto_ms"]
        self.max_rto = config["max_rto_ms"]
        self.initial_rto = config["initial_rto_ms"]
        self.srtt_q8 = None
        self.rttvar_q4 = None

    @property
    def srtt_ms(self):
        return None if self.srtt_q8 is None else self.srtt_q8 >> 3

    @property
    def rttvar_ms(self):
        return None if self.rttvar_q4 is None else self.rttvar_q4 >> 2

    @property
    def rto_ms(self):
        if self.srtt_q8 is None:
            return self.initial_rto
        rto = (self.srtt_q8 >> 3) + max(self.granularity, self.rttvar_q4)
        return max(self.min_rto, min(self.max_rto, rto))

    def add_sample(self, r):
        if r <= 0:
            raise BizError(E_CLOCK, f"non-positive RTT sample {r}ms rejected")
        if self.srtt_q8 is None:
            self.srtt_q8 = r << 3
            self.rttvar_q4 = r << 1  # (R/2) << 2
            return
        err_q8 = abs(self.srtt_q8 - (r << 3))
        self.rttvar_q4 = (3 * self.rttvar_q4 + (err_q8 >> 1)) >> 2
        self.srtt_q8 = (7 * self.srtt_q8 + (r << 3)) >> 3
