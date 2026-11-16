"""Business error codes for the config distribution platform."""

E_MANIFEST = "E_MANIFEST"    # manifest validation failed, whole manifest rejected
E_NODE = "E_NODE"            # node process crashed or spoke invalid protocol
E_CONVERGE = "E_CONVERGE"    # receipts did not converge to expected derived state
E_REPLAY = "E_REPLAY"        # post-recovery recomputation is not reproducible
E_STATE = "E_STATE"          # illegal state-machine transition / commit blocked


class PlatformError(Exception):
    """Error carrying a business code and structured details."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
