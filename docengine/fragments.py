"""Content-addressed fragment directory access with checksum verification."""

import os

from .errors import BusinessError, ErrorCode
from .stablejson import bytes_sha256


class FragmentStore:
    def __init__(self, fragment_dir, manifest_fragments):
        self.dir = fragment_dir
        # checksum -> {"size": int, "sha256": str}
        self.manifest = {f["checksum"]: f for f in manifest_fragments}

    def path_for(self, checksum):
        return os.path.join(self.dir, checksum)

    def size_of(self, checksum):
        entry = self.manifest.get(checksum)
        if entry is not None:
            return entry["size"]
        path = self.path_for(checksum)
        if os.path.exists(path):
            return os.path.getsize(path)
        return None

    def read_verified(self, checksum):
        """Read a fragment and verify content against its checksum and the
        manifest digest. Raises E_FRAGMENT_CHECKSUM on any mismatch."""
        path = self.path_for(checksum)
        if not os.path.exists(path):
            raise BusinessError(
                ErrorCode.E_FRAGMENT_CHECKSUM,
                "fragment missing from directory",
                checksum=checksum,
                reason="missing",
            )
        with open(path, "rb") as fh:
            data = fh.read()
        actual = bytes_sha256(data)
        entry = self.manifest.get(checksum)
        if actual != checksum:
            raise BusinessError(
                ErrorCode.E_FRAGMENT_CHECKSUM,
                "fragment content does not match its content address",
                checksum=checksum,
                actual_sha256=actual,
                reason="content_address_mismatch",
            )
        if entry is not None and actual != entry["sha256"]:
            raise BusinessError(
                ErrorCode.E_FRAGMENT_CHECKSUM,
                "fragment content does not match manifest digest",
                checksum=checksum,
                actual_sha256=actual,
                manifest_sha256=entry["sha256"],
                reason="manifest_mismatch",
            )
        return data

    def fetch_span(self, checksum, offset, length, doc_id=None, field=None):
        """Fetch a byte span from a verified fragment; returns the slice."""
        try:
            data = self.read_verified(checksum)
        except BusinessError as exc:
            exc.details.setdefault("doc_id", doc_id)
            exc.details.setdefault("field", field)
            exc.details.setdefault("offset", offset)
            exc.details.setdefault("length", length)
            raise
        if offset < 0 or length < 0 or offset + length > len(data):
            raise BusinessError(
                ErrorCode.E_V1_SPAN_RANGE,
                "span out of fragment bounds",
                doc_id=doc_id,
                field=field,
                checksum=checksum,
                offset=offset,
                length=length,
                fragment_size=len(data),
            )
        return data[offset : offset + length]
