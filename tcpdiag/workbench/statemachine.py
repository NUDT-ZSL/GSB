"""教学用 TCP 状态机。

合法路径：CLOSED -> SYN_SENT -> ESTABLISHED -> FIN_WAIT -> CLOSE_WAIT -> CLOSED
另允许对端主动关闭：ESTABLISHED --RCV_FIN--> CLOSE_WAIT。
任何未列出的 (状态, 触发) 组合均以 E_STATE 拒绝并保持原状态。
"""

from .errors import E_STATE, BizError


class TCPStateMachine:
    STATES = ("CLOSED", "SYN_SENT", "ESTABLISHED", "FIN_WAIT", "CLOSE_WAIT")

    TRANSITIONS = {
        ("CLOSED", "SYN"): "SYN_SENT",
        ("SYN_SENT", "SYN_ACK"): "ESTABLISHED",
        ("ESTABLISHED", "FIN"): "FIN_WAIT",
        ("ESTABLISHED", "RCV_FIN"): "CLOSE_WAIT",
        ("FIN_WAIT", "RCV_FIN"): "CLOSE_WAIT",
        ("CLOSE_WAIT", "FIN_ACK"): "CLOSED",
    }

    def __init__(self):
        self.state = "CLOSED"

    def transition(self, trigger, seq=None, now=None):
        key = (self.state, trigger)
        if key not in self.TRANSITIONS:
            raise BizError(
                E_STATE,
                f"illegal transition: state={self.state} trigger={trigger} seq={seq}",
            )
        old = self.state
        self.state = self.TRANSITIONS[key]
        return old, self.state

    def force(self, state):
        """快照恢复专用：整体替换状态，不经过迁移校验。"""
        if state not in self.STATES:
            raise BizError(E_STATE, f"unknown state {state!r} in snapshot")
        self.state = state
