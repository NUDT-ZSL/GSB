"""Generate the deterministic PCM / truth / manifest corpus (entry point)."""

from __future__ import annotations

import argparse
import json

from seraph_core.data.synth import generate_corpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", default="config/corpus_spec.json")
    parser.add_argument("--out", default="data/corpus")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=float, default=20.0)
    parser.add_argument("--hop-ms", type=float, default=10.0)
    args = parser.parse_args()
    result = generate_corpus(args.spec, args.out, args.sample_rate,
                             args.frame_ms, args.hop_ms)
    print(json.dumps({
        "dialogues": len(result["manifest"]),
        "manifest_path": result["manifest_path"],
        "out_dir": result["out_dir"],
    }, indent=2))


if __name__ == "__main__":
    main()
