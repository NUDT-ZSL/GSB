"""Business-facing error types."""


class SceneError(Exception):
    """The scene JSON is missing, malformed, or internally inconsistent."""


class OutputError(Exception):
    """Preview/result files could not be written safely."""
