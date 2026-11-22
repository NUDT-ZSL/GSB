"""Business error codes and StoreError exception type."""

E_BAD_SAMPLE = "E_BAD_SAMPLE"
E_CHUNK_MISSING = "E_CHUNK_MISSING"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_OBJECT_NOT_COMMITTED = "E_OBJECT_NOT_COMMITTED"
E_CACHE_CAPACITY = "E_CACHE_CAPACITY"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
E_BUDGET_EXHAUSTED = "E_BUDGET_EXHAUSTED"
E_CONSISTENCY_ANOMALY = "E_CONSISTENCY_ANOMALY"


class StoreError(Exception):
    """Exception carrying a business error code and diagnostic fields."""

    def __init__(self, code, message, **details):
        super().__init__(code, message)  # args 与签名一致，保证可跨进程 pickle
        self.code = code
        self.message = message
        self.details = details

    def __str__(self):
        return f"{self.code}: {self.message}"

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
