"""Node registry loading."""

import json

from .errors import E_MANIFEST, PlatformError


def load_registry(path):
    """Load the node registry file; returns the ordered list of node ids."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise PlatformError(E_MANIFEST, "cannot load node registry %s: %s" % (path, exc))
    nodes = body.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PlatformError(E_MANIFEST, "registry %s: 'nodes' must be a non-empty array" % path)
    ids = []
    for idx, entry in enumerate(nodes):
        node_id = entry.get("id") if isinstance(entry, dict) else None
        if not node_id or not isinstance(node_id, str):
            raise PlatformError(
                E_MANIFEST, "registry %s: nodes[%d].id missing or invalid" % (path, idx)
            )
        if node_id in ids:
            raise PlatformError(
                E_MANIFEST, "registry %s: duplicate node id %s" % (path, node_id)
            )
        ids.append(node_id)
    return ids
