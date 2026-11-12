"""Deep-parse worker executed in a child process.

The worker extracts VBA modules from a vbaProject/OLE container, normalises
them and extracts features, streaming one message per module back through
the queue so the parent can retain partial results when a timeout kills the
parse. Message types: started, module, done, fatal.
"""

from .features import extract_features
from .normalize import normalize_source
from .vbaproject import list_modules, open_container, read_module_source


def entry(container_bytes: bytes, prefix_hint, queue):
    try:
        queue.put({"type": "started"})
        ole, prefix = open_container(container_bytes)
        try:
            metas = list_modules(ole, prefix)
            for meta in metas:
                name = meta.get("name", "?")
                try:
                    source = read_module_source(ole, prefix, meta)
                    text = source.decode("cp1252")
                    statements = normalize_source(text)
                    features = extract_features(name, statements)
                    queue.put({
                        "type": "module",
                        "module": {
                            "name": name,
                            "status": "ok",
                            "normalized": [s["text"] for s in statements],
                            "errors": [],
                        },
                        "features": features,
                    })
                except Exception as exc:
                    queue.put({
                        "type": "module",
                        "module": {
                            "name": name,
                            "status": "decode_error",
                            "normalized": [],
                            "errors": ["%s: %s" % (type(exc).__name__, exc)],
                        },
                        "features": [],
                    })
        finally:
            ole.close()
        queue.put({"type": "done", "errors": []})
    except Exception as exc:
        queue.put({"type": "fatal",
                   "message": "%s: %s" % (type(exc).__name__, exc)})
