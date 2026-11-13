"""服务与检测器共享的常量。"""

from __future__ import annotations

# 仅用于本地演示的共享令牌；生产环境应从受控配置/密钥库加载。
DEFAULT_SHARED_TOKEN = "demo-shared-token-2026"

QUERY_PATH = "/api/accounts/query"
HEALTH_PATH = "/health"

TOKEN_FIELD = "token"
ACCOUNT_FIELD = "account_id"

# 修复版限制：请求体大小与字段长度。
MAX_BODY_BYTES = 4096
MAX_ACCOUNT_ID_LENGTH = 64

WRONG_TOKEN_FOR_PROBE = "wrong-token-for-detection-0001"
VALID_ACCOUNT_ID = "ACC-1001"
