"""Business error codes (E_xxx) for the inference gateway."""

E_SCHEMA_SERVICE = "E_SCHEMA_SERVICE"
E_SCHEMA_NODE = "E_SCHEMA_NODE"
E_SCHEMA_TRACE = "E_SCHEMA_TRACE"
E_DUPLICATE_SERVICE = "E_DUPLICATE_SERVICE"
E_DUPLICATE_NODE = "E_DUPLICATE_NODE"
E_UNKNOWN_SERVICE = "E_UNKNOWN_SERVICE"
E_PLACEMENT_VRAM = "E_PLACEMENT_VRAM"
E_PLACEMENT_CONCURRENCY = "E_PLACEMENT_CONCURRENCY"
E_PLACEMENT_DOMAIN = "E_PLACEMENT_DOMAIN"
E_TOKEN_BUDGET = "E_TOKEN_BUDGET"
E_ROUND_TIME_BUDGET = "E_ROUND_TIME_BUDGET"
E_UPSTREAM_CONN_REFUSED = "E_UPSTREAM_CONN_REFUSED"
E_UPSTREAM_BUDGET = "E_UPSTREAM_BUDGET"
E_UPSTREAM_DIGEST = "E_UPSTREAM_DIGEST"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"


class GatewayError(Exception):
    """Business error carrying a stable E_xxx code and diagnostic fields."""

    def __init__(self, code, message, **fields):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fields = fields

    def to_dict(self):
        return {"error": self.code, "message": self.message, "fields": self.fields}
