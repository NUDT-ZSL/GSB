import unittest
from pathlib import Path

from gateway.consistency import classify_boundary, compare_traces
from gateway.engine import GatewayEngine
from gateway.errors import E_AUTH, E_LIMIT, E_OK, E_VERSION
from gateway.limiters import PathDecision
from gateway.loader import load_requests, load_routes, load_token_table
from gateway.models import (
    PATH_TOKEN_BUCKET,
    PATH_WINDOW_COUNTER,
    PATH_WINDOW_LOG,
    Request,
    TokenTable,
)
from gateway.selector import WORKLOAD_EPSILON, select_path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def build(sample: str) -> tuple[GatewayEngine, list[Request]]:
    routes_version, routes = load_routes(SAMPLES / sample / "routes.json")
    table = load_token_table(SAMPLES / sample / "tokens.json")
    requests = load_requests(SAMPLES / sample / "requests.ndjson", routes)
    return GatewayEngine(routes, routes_version, table), requests


class EngineTest(unittest.TestCase):
    def test_auth_runs_before_limit_and_never_consumes_quota(self) -> None:
        engine, requests = build("basic")
        for request in requests:
            engine.process(request)
        failed = [row for row in engine.trace if row["decision"] == E_AUTH]
        self.assertEqual(len(failed), 3)
        for row in failed:
            for path_decision in row["paths"].values():
                self.assertEqual(path_decision["reason"], "skipped_after_auth_failure")
                self.assertIsNone(path_decision["remaining"])

    def test_boundary_sample_records_attributions(self) -> None:
        engine, requests = build("boundary")
        for request in requests:
            engine.process(request)
        report = compare_traces(engine.trace, engine.routes_by_name)
        self.assertFalse(report["consistent"])
        boundaries = {item["boundary"] for item in report["divergences"]}
        self.assertIn("refill_truncation", boundaries)
        self.assertIn("window_alignment", boundaries)
        for item in report["divergences"]:
            self.assertEqual(set(item["decisions"].values()), {E_OK, E_LIMIT})

    def test_attribution_is_position_independent_per_client(self) -> None:
        # Two clients on the same route with identical relative timestamps.
        # Interleaving their requests must not change either client's per-request
        # decisions: per-client state is independent of global traversal order.
        engine, requests = build("boundary")
        for request in requests:
            engine.process(request)
        first_client = {row["seq"]: row["paths"] for row in engine.trace}

        engine_b, requests_b = build("boundary")
        from gateway.models import TokenRecord
        engine_b.authenticator.token_table.tokens["tok-edge-2"] = TokenRecord(
            "tok-edge-2", "second-client", 1_000_000
        )
        shifted = [
            Request(
                seq=request.seq + 1000,
                route=request.route,
                client_id="second-client",
                token="tok-edge-2",
                ts_ms=request.ts_ms + 5000,
                signed_at_ms=request.signed_at_ms,
                signature=request.signature,
                source_line=request.source_line,
            )
            for request in requests_b
        ]
        interleaved = sorted(
            list(requests_b) + shifted, key=lambda item: item.ts_ms
        )
        for request in interleaved:
            engine_b.process(request)
        second_client = {
            row["seq"] - 1000: row["paths"]
            for row in engine_b.trace
            if row["client_id"] == "second-client"
        }
        self.assertEqual(set(first_client), set(second_client))
        for seq in first_client:
            for path_name, decision in first_client[seq].items():
                self.assertEqual(
                    decision["allow"], second_client[seq][path_name]["allow"]
                )
                self.assertEqual(
                    decision["reason"], second_client[seq][path_name]["reason"]
                )

    def test_policy_evolution_replaces_state_wholesale(self) -> None:
        engine, requests = build("basic")
        for request in requests[:5]:
            engine.process(request)
        self.assertGreater(engine.authenticator.cache_size(), 0)
        new_routes_version, new_routes = load_routes(SAMPLES / "basic" / "routes.json")
        new_table = load_token_table(SAMPLES / "basic" / "tokens.json")
        new_table.version = 2
        cached_before = engine.authenticator.cache_size()
        replacement = engine.replace_policy(new_routes, new_routes_version + 1, new_table)
        self.assertEqual(replacement["cache_entries_cleared"], cached_before)
        self.assertEqual(engine.limiter_sets, {})
        self.assertEqual(engine.trace, [])
        self.assertEqual(engine.current_version, (2, 2))
        with self.assertRaises(ValueError):
            engine.replace_policy(new_routes, 1, new_table)

    def test_stale_history_kept_separately(self) -> None:
        engine, requests = build("basic")
        for request in requests:
            engine.process(request)
        new_table = load_token_table(SAMPLES / "basic" / "tokens.json")
        new_table.version = 2
        _, new_routes = load_routes(SAMPLES / "basic" / "routes.json")
        engine.replace_policy(new_routes, 2, new_table)
        self.assertEqual(len(engine.history), 1)
        stale = engine.history[0]
        self.assertEqual(stale.policy_version, (1, 1))
        self.assertNotEqual(stale.trace, engine.trace)

    def test_versioned_summary_marks_stale_decisions(self) -> None:
        engine, requests = build("basic")
        for request in requests:
            engine.process(request)
        new_table = load_token_table(SAMPLES / "basic" / "tokens.json")
        new_table.version = 2
        _, new_routes = load_routes(SAMPLES / "basic" / "routes.json")
        engine.replace_policy(new_routes, 2, new_table)
        for request in requests[:3]:
            engine.process(request)
        summary = engine.versioned_summary()
        self.assertEqual(summary["current_policy_version"], [2, 2])
        self.assertEqual(summary["current_request_count"], 3)
        self.assertEqual(summary["stale_request_count"], len(requests))
        self.assertEqual(summary["stale_versions"], [(1, 1)])
        self.assertTrue(summary["stale_rows_excluded_from_summary"])


class SelectorTest(unittest.TestCase):
    def test_deterministic_selection_and_tie_break(self) -> None:
        _, routes = load_routes(SAMPLES / "large" / "routes.json")
        route = routes[0]
        first = select_path(route, client_count=120, span_ms=3000, observed_max_log=5)
        second = select_path(route, client_count=120, span_ms=3000, observed_max_log=5)
        self.assertEqual(first.path, second.path)
        self.assertIn(first.path, (
            PATH_TOKEN_BUCKET, PATH_WINDOW_COUNTER, PATH_WINDOW_LOG
        ))
        # Epsilon is an explicit, documented constant.
        self.assertGreater(WORKLOAD_EPSILON, 0)

    def test_large_scale_eliminates_log_path(self) -> None:
        _, routes = load_routes(SAMPLES / "large" / "routes.json")
        selection = select_path(routes[0], client_count=200, span_ms=5000,
                                observed_max_log=routes[0].max_in_window)
        self.assertNotEqual(selection.path, PATH_WINDOW_LOG)
        self.assertTrue(any("eliminated" in reason for reason in selection.reasons))

    def test_classify_boundary_pure_function(self) -> None:
        decisions = {
            PATH_TOKEN_BUCKET: PathDecision(PATH_TOKEN_BUCKET, True, 1, "", ""),
            PATH_WINDOW_COUNTER: PathDecision(PATH_WINDOW_COUNTER, False, 0, "", ""),
            PATH_WINDOW_LOG: PathDecision(PATH_WINDOW_LOG, False, 0, "", ""),
        }
        first = classify_boundary(decisions, 500, 650)
        second = classify_boundary(decisions, 500, 650)
        self.assertEqual(first, second)
        self.assertEqual(first[0], "refill_truncation")


if __name__ == "__main__":
    unittest.main()
