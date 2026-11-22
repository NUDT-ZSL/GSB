from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eventgraph.generator import DocumentGenerator


def main() -> None:
    DocumentGenerator(ROOT).generate_all()
    manifest = json.loads(
        (ROOT / "data" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    print(
        f"generated {manifest['document_count']} documents "
        f"with manifest {manifest['manifest_sha256']}"
    )
    for document in manifest["documents"]:
        print(
            f"{document['doc_id']} "
            f"content={document['content_sha256'][:16]} "
            f"file={document['file_sha256'][:16]}"
        )


if __name__ == "__main__":
    main()
