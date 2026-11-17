"""Business error codes shared across the OCR workbench."""

E_IMAGE_DECODE = "E_IMAGE_DECODE"
E_IMAGE_SHAPE = "E_IMAGE_SHAPE"
E_TRUTH_OUT_OF_RANGE = "E_TRUTH_OUT_OF_RANGE"
E_TRUTH_INVALID = "E_TRUTH_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_INTERACT_BUDGET_PENDING = "E_INTERACT_BUDGET_PENDING"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_UNKNOWN_COMMAND = "E_UNKNOWN_COMMAND"


class WorkbenchError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
