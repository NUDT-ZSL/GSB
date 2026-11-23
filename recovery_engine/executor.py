"""动作执行器：确定性故障注入点。

真实环境中这里对接目标系统；样本验证中使用 SimulatedExecutor，
按注入表确定性地模拟：连接拒绝（E_CONN_REFUSED）、
超出单次逻辑预算（E_BUDGET_EXCEEDED）、或在提交若干动作后
模拟进程被强制终止（crash_after_commits，用于恢复一致性验证）。
"""

from .errors import BizError, ErrCode


class SimulatedExecutor:
    """确定性模拟执行器。

    - failures: {action_id: "conn_refused" | "budget"} 注入故障；
    - budget_ms: 单次动作逻辑预算，动作 cost_ms 超过即拒绝；
    - action_costs: {action_id: cost_ms}，缺省 0；
    - crash_after_commits: 在本进程累计提交 N 个动作后 os._exit 模拟强杀；
    - executed: 已执行动作日志 [(service, action_id, clock_ms)]，供断言。
    """

    def __init__(self, failures=None, budget_ms=None, action_costs=None,
                 crash_after_commits=None):
        self.failures = dict(failures or {})
        self.budget_ms = budget_ms
        self.action_costs = dict(action_costs or {})
        self.crash_after_commits = crash_after_commits
        self.executed = []
        self._commit_count = 0

    def execute(self, service, action, clock_ms):
        failure = self.failures.get(action.action_id)
        if failure == "conn_refused":
            raise BizError(ErrCode.E_CONN_REFUSED, "动作目标连接拒绝",
                           service=service, action_id=action.action_id)
        if failure == "budget":
            raise BizError(ErrCode.E_BUDGET_EXCEEDED, "动作超出单次逻辑预算",
                           service=service, action_id=action.action_id)
        cost = self.action_costs.get(action.action_id, 0)
        if self.budget_ms is not None and cost > self.budget_ms:
            raise BizError(ErrCode.E_BUDGET_EXCEEDED, "动作超出单次逻辑预算",
                           service=service, action_id=action.action_id,
                           cost_ms=cost, budget_ms=self.budget_ms)
        self.executed.append((service, action.action_id, clock_ms))
        self._commit_count += 1
        if self.crash_after_commits is not None and self._commit_count >= self.crash_after_commits:
            import os
            import signal
            os.kill(os.getpid(), signal.SIGKILL)
        return {"status": "ok", "cost_ms": cost}


class SimulatedHealth:
    """确定性健康结论源：healthy_rungs[service] = 通过健康校验的最高阶梯序号。"""

    def __init__(self, healthy_rungs=None):
        self.healthy_rungs = dict(healthy_rungs or {})

    def is_healthy(self, service, rung_index):
        return rung_index <= self.healthy_rungs.get(service, 10**9)
