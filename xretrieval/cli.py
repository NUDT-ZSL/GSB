"""Command line interface for the cross-lingual retrieval engine."""

import argparse
import json
import sys

from .bridge import BridgeResource
from .canon import canonical_text
from .engine import Engine
from .errors import XError
from .samplegen import generate_all, generate_bad_samples
from .validate import Verifier


def _emit(obj):
    print(canonical_text(obj))


def cmd_gen_sample(args):
    meta = generate_all(args.out)
    _emit({"ok": True, "meta": meta})


def cmd_gen_bad_samples(args):
    generate_bad_samples(args.out)
    _emit({"ok": True, "out": args.out})


def cmd_build(args):
    engine = Engine(args.state)
    report = engine.build(args.resource, args.docs, recover=args.recover,
                          simulate_crash_after=args.simulate_crash_after)
    _emit({"ok": True, "report": report})


def cmd_query(args):
    engine = Engine(args.state)
    results = engine.run_query_file(args.queries, args.out,
                                    workers=args.workers)
    _emit({"ok": True, "queries": len(results), "out": args.out})


def cmd_expand(args):
    resource = BridgeResource.load(args.resource)
    candidates = resource.expand_term(args.query_lang, args.term,
                                      args.target_lang)
    _emit({"ok": True, "candidates": candidates})


def cmd_verify(args):
    verifier = Verifier(args.state)
    checks = [verifier.verify_partitions(args.docs)]
    if args.queries and args.results:
        checks.append(verifier.verify_results(args.queries, args.results))
    _emit({"ok": True, "checks": checks})


def cmd_selfcheck(args):
    verifier = Verifier(args.state)
    _emit(verifier.selfcheck(args.docs))


def cmd_demo(args):
    from .demo import run_demo
    report = run_demo(args.out)
    _emit({"ok": report["summary"]["failed"] == 0,
           "report": f"{args.out}/verification_report.json"})


def build_parser():
    parser = argparse.ArgumentParser(prog="xretrieval")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gen-sample", help="generate deterministic samples")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_gen_sample)

    p = sub.add_parser("gen-bad-samples", help="generate invalid samples")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_gen_bad_samples)

    p = sub.add_parser("build", help="run one build batch")
    p.add_argument("--state", required=True)
    p.add_argument("--resource", required=True)
    p.add_argument("--docs", required=True)
    p.add_argument("--recover", action="store_true",
                   help="discard a stale build lock left by a killed build")
    p.add_argument("--simulate-crash-after", type=int, default=None,
                   help="hard-exit after N partition commits (testing)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("query", help="run queries against committed state")
    p.add_argument("--state", required=True)
    p.add_argument("--queries", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("expand", help="expand one term for inspection")
    p.add_argument("--resource", required=True)
    p.add_argument("--query-lang", required=True)
    p.add_argument("--target-lang", required=True)
    p.add_argument("--term", required=True)
    p.set_defaults(func=cmd_expand)

    p = sub.add_parser("verify", help="independently verify state and results")
    p.add_argument("--state", required=True)
    p.add_argument("--docs", required=True)
    p.add_argument("--queries")
    p.add_argument("--results")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("selfcheck", help="cross-partition consistency check")
    p.add_argument("--state", required=True)
    p.add_argument("--docs", required=True)
    p.set_defaults(func=cmd_selfcheck)

    p = sub.add_parser("demo", help="run the full acceptance demo")
    p.add_argument("--out", default="artifacts")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except XError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()},
                         ensure_ascii=False, sort_keys=True),
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
