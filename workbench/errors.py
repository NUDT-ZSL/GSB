"""Business error codes used across the workbench."""

E_REGION_INVALID = "E_REGION_INVALID"
E_PARSE_INVALID = "E_PARSE_INVALID"
E_CONFIDENCE_RANGE = "E_CONFIDENCE_RANGE"
E_RELATION_DANGLING = "E_RELATION_DANGLING"
E_BOUNDARY_SENSITIVE = "E_BOUNDARY_SENSITIVE"
E_IMAGE_INVALID = "E_IMAGE_INVALID"


class WorkbenchError(Exception):
    """Exception carrying a business error code."""

    def __init__(self, code: str, detail: str, location=None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.location = location
