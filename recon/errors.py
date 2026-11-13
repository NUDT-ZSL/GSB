"""业务错误码与异常体系。

错误码均为稳定字符串，供运行报告与进程 stderr 输出使用。
retryable 标记异常是否属于分级重试的可重试错误。
"""


class ReconError(Exception):
    code = "E_UNKNOWN"
    retryable = False

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


# ---------- 配置 / 锁 ----------

class ConfigError(ReconError):
    def __init__(self, code: str, message: str):
        super().__init__(message, code=code)


class LockError(ReconError):
    code = "E_LOCKED"
    retryable = False


# ---------- 数据源错误 ----------

class NetError(ReconError):
    """连接错误 / 超时，可重试。"""
    code = "E_CONN"
    retryable = True


class RateLimitedError(ReconError):
    """HTTP 429，可重试。"""
    code = "E_HTTP_429"
    retryable = True


class ServerError(ReconError):
    """HTTP 5xx，可重试。"""
    code = "E_HTTP_5XX"
    retryable = True


class BadRequestError(ReconError):
    """HTTP 400，不可重试。"""
    code = "E_BAD_REQUEST"
    retryable = False


class AuthError(ReconError):
    """HTTP 401，不可重试。"""
    code = "E_AUTH"
    retryable = False


class DecodeError(ReconError):
    """JSON 解码失败，不可重试。"""
    code = "E_DECODE"
    retryable = False


class ValidationError(ReconError):
    """字段校验失败，不可重试。"""
    code = "E_VALIDATION"
    retryable = False


class CsvPartialError(ReconError):
    """CSV 快照被部分写入（末尾截断 / 不完整行），可重试。"""
    code = "E_CSV_PARTIAL"
    retryable = True


class CircuitOpenError(ReconError):
    """熔断打开，当前源终止。"""
    code = "E_CIRCUIT_OPEN"
    retryable = False


class StoppedError(ReconError):
    """受控中断（控制文件 / 信号），等待被打断时抛出。"""
    code = "E_STOPPED"
    retryable = False


# 非重试 HTTP 状态码 -> 业务码映射
FATAL_HTTP_CODES = {400: "E_BAD_REQUEST", 401: "E_AUTH", 403: "E_FORBIDDEN", 404: "E_NOT_FOUND"}
