"""Business error codes and failure records."""

E_CORPUS_INVALID = "E_CORPUS_INVALID"
E_MODEL_SHAPE_MISMATCH = "E_MODEL_SHAPE_MISMATCH"
E_DICT_INVALID = "E_DICT_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"


def failure(item_id, stage, code, exc_type, message=""):
    return {
        "id": item_id,
        "stage": stage,
        "code": code,
        "exc_type": exc_type,
        "message": str(message),
    }
