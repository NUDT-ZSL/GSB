"""业务错误码、异常与失败清单条目。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


E_EMPTY_DOC = "E_EMPTY_DOC"
E_PARSE_INVALID = "E_PARSE_INVALID"
E_DUP_DOC_ID = "E_DUP_DOC_ID"
E_POSITION_UNAVAILABLE = "E_POSITION_UNAVAILABLE"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_ENCODE_FAILED = "E_ENCODE_FAILED"
E_DECODE_FAILED = "E_DECODE_FAILED"
E_QUERY_FAILED = "E_QUERY_FAILED"
E_BUILD_FAILED = "E_BUILD_FAILED"


class WorkbenchError(Exception):
    """工作台业务异常基类，携带业务码与阶段名。"""

    code = "E_BUILD_FAILED"

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        identifier: str | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.identifier = identifier
        if code is not None:
            self.code = code


@dataclass(frozen=True)
class FailureEntry:
    """单条文档/查询/块的失败记录。"""

    identifier: str
    stage: str
    code: str
    exception_type: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_failure(
    identifier: str,
    stage: str,
    code: str,
    exc: BaseException | str,
) -> FailureEntry:
    if isinstance(exc, BaseException):
        message = str(exc) or exc.__class__.__name__
        exc_type = type(exc).__name__
    else:
        message = exc
        exc_type = "BusinessRule"
    return FailureEntry(
        identifier=str(identifier),
        stage=stage,
        code=code,
        exception_type=exc_type,
        message=message,
    )
