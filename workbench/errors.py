"""Error codes and structured exceptions for the SDF mesh workbench.

Every validation / CSG domain failure raises a WorkbenchError carrying a
stable machine-readable code plus location info (asset name, boolean-tree
node path, parameter name) so the API/CLI can report exactly where the
problem is.  Failed operations must never leave half-built meshes or
corrupt existing version records.
"""


class WorkbenchError(Exception):
    """Structured error with a stable code and location info."""

    def __init__(self, code, message, asset=None, node_path=None, param=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.asset = asset
        self.node_path = node_path
        self.param = param

    def to_dict(self):
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "asset": self.asset,
                "node_path": self.node_path,
                "param": self.param,
            }
        }


# Parameter validation (requirement 8)
E_INVALID_RESOLUTION = "E_INVALID_RESOLUTION"
E_INVALID_THRESHOLD = "E_INVALID_THRESHOLD"
E_INVALID_TARGET_FACES = "E_INVALID_TARGET_FACES"
E_INVALID_TOLERANCE = "E_INVALID_TOLERANCE"
E_INVALID_PRIMITIVE = "E_INVALID_PRIMITIVE"
E_INVALID_OPERATOR = "E_INVALID_OPERATOR"

# CSG domain mismatches (requirement 8)
E_CSG_EMPTY_INTERSECTION = "E_CSG_EMPTY_INTERSECTION"
E_CSG_EMPTY_SUBTRAHEND = "E_CSG_EMPTY_SUBTRAHEND"
E_CSG_EMPTY_RESULT = "E_CSG_EMPTY_RESULT"

# Lookup / state errors
E_ASSET_NOT_FOUND = "E_ASSET_NOT_FOUND"
E_VERSION_NOT_FOUND = "E_VERSION_NOT_FOUND"


def validate_constraints(asset):
    """Validate per-asset constraint parameters.

    Resolution, iso threshold and target face counts that are zero or
    negative are rejected with explicit error codes before any work is
    done, so no half-built mesh can be produced.
    """
    name = asset.get("name")
    c = asset.get("constraints", {})

    res = c.get("resolution")
    if res is None or res <= 0:
        raise WorkbenchError(
            E_INVALID_RESOLUTION,
            f"resolution must be a positive integer, got {res!r}",
            asset=name, param="resolution",
        )
    iso = c.get("iso_threshold")
    if iso is None or iso <= 0:
        raise WorkbenchError(
            E_INVALID_THRESHOLD,
            f"iso_threshold must be positive, got {iso!r}",
            asset=name, param="iso_threshold",
        )
    target = c.get("target_faces")
    if target is None or target <= 0:
        raise WorkbenchError(
            E_INVALID_TARGET_FACES,
            f"target_faces must be a positive integer, got {target!r}",
            asset=name, param="target_faces",
        )
    for key in ("base_tolerance", "min_wall_thickness",
                "volume_tolerance", "deviation_tolerance"):
        val = c.get(key)
        if val is None or val <= 0:
            raise WorkbenchError(
                E_INVALID_TOLERANCE,
                f"{key} must be positive, got {val!r}",
                asset=name, param=key,
            )
