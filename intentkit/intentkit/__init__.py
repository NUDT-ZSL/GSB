"""intentkit：本地确定性中文客服工单意图识别组件。

所有判定均为纯函数式、内容寻址，不依赖系统时间、字典遍历顺序或外部服务。
"""

from .errors import BizError, CODES
from .models import IntentConfig, IntentRule, Ticket, canonical_json, sha256_hex

__all__ = [
    "BizError",
    "CODES",
    "IntentConfig",
    "IntentRule",
    "Ticket",
    "canonical_json",
    "sha256_hex",
]

__version__ = "1.0.0"
