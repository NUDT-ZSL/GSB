"""chunkmeta：本地对象存储分片与元数据管理组件。"""
from .errors import (
    E_BAD_SAMPLE,
    E_BUDGET_EXHAUSTED,
    E_CACHE_CAPACITY,
    E_CHUNK_MISSING,
    E_CLOCK_REGRESSION,
    E_CONSISTENCY_ANOMALY,
    E_DIGEST_MISMATCH,
    E_OBJECT_NOT_COMMITTED,
    StoreError,
)
from .samples import (
    BucketDecl,
    ObjectManifest,
    StoreConfig,
    load_samples,
)
from .service import StoreService

__all__ = [
    "StoreService",
    "StoreError",
    "load_samples",
    "BucketDecl",
    "ObjectManifest",
    "StoreConfig",
    "E_BAD_SAMPLE",
    "E_CHUNK_MISSING",
    "E_DIGEST_MISMATCH",
    "E_OBJECT_NOT_COMMITTED",
    "E_CACHE_CAPACITY",
    "E_CLOCK_REGRESSION",
    "E_BUDGET_EXHAUSTED",
    "E_CONSISTENCY_ANOMALY",
]

__version__ = "0.1.0"
