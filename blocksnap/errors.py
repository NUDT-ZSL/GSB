"""Domain business error codes.

Every validation or runtime failure inside blocksnap is represented by a
specific business code string. The four codes are part of the public contract:

* ``E_VOLUME``    - volume image / block geometry / block index validation
* ``E_SNAPSHOT``  - snapshot naming, bitmap geometry, manifest constraints
* ``E_INTEGRITY`` - referenced block data digest mismatch (corruption)
* ``E_CHAIN``     - missing snapshot chain reference or un-replayable chain
"""

E_VOLUME = "E_VOLUME"
E_SNAPSHOT = "E_SNAPSHOT"
E_INTEGRITY = "E_INTEGRITY"
E_CHAIN = "E_CHAIN"


class BlockSnapError(Exception):
    """Base class carrying a stable business code and structured context."""

    code = "E_UNKNOWN"

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = dict(context)

    def to_record(self):
        return {"code": self.code, "message": self.message, "context": self.context}


class VolumeError(BlockSnapError):
    code = E_VOLUME


class SnapshotError(BlockSnapError):
    code = E_SNAPSHOT


class IntegrityError(BlockSnapError):
    code = E_INTEGRITY


class ChainError(BlockSnapError):
    code = E_CHAIN
