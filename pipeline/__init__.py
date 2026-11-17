"""安全策略交互一致性验证管线。

仅使用标准库（concurrent.futures / json / threading 等）实现：
解析规范化、本地确定性校验服务、交互延迟采集、响应一致性核对、
资源预算与队列上限控制、检查点恢复与可复现 JSON 报告。
"""

__all__ = ["errors", "normalize", "localcalc", "service", "checkpoint", "engine"]
