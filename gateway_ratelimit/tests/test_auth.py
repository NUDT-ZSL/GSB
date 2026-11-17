import unittest

from gateway.auth import Authenticator
from gateway.errors import E_AUTH
from gateway.models import (
    Request,
    Route,
    SignatureRecord,
    TokenRecord,
    TokenTable,
)


def token_table() -> TokenTable:
    table = TokenTable(version=1)
    table.tokens["tok-a"] = TokenRecord("tok-a", "alice", 1000)
    table.tokens["tok-b"] = TokenRecord("tok-b", "bob", 1000)
    table.signatures["key-1"] = SignatureRecord("key-1", "dave", "secret", 10_000_000)
    return table


def token_route() -> Route:
    return Route("r", "/r", "token", 1, 1, 100, 1)


def signature_route() -> Route:
    return Route("p", "/p", "signature", 1, 1, 100, 1)


def request(**overrides) -> Request:
    base = dict(seq=1, route="r", client_id="alice", token="tok-a", ts_ms=500)
    base.update(overrides)
    return Request(**base)


class AuthenticatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = Authenticator(token_table(), routes_version=1)

    def test_token_happy_path_and_cache_hit(self) -> None:
        route = token_route()
        first = self.auth.verify(route, request())
        self.assertTrue(first.ok)
        self.assertFalse(first.cache_hit)
        second = self.auth.verify(route, request())
        self.assertTrue(second.ok)
        self.assertTrue(second.cache_hit)

    def test_binding_unknown_and_expired_failures(self) -> None:
        route = token_route()
        self.assertEqual(
            self.auth.verify(route, request(client_id="mallory")).code, E_AUTH
        )
        self.assertEqual(
            self.auth.verify(route, request(token="nope")).code, E_AUTH
        )
        self.assertEqual(
            self.auth.verify(route, request(ts_ms=2000)).code, E_AUTH
        )

    def test_cached_and_direct_results_always_agree(self) -> None:
        route = token_route()
        # Failure cases first so the positive cache entry cannot interfere.
        cases = [
            request(client_id="mallory"),
            request(token="nope"),
            request(ts_ms=2000),
            request(ts_ms=900),
        ]
        for item in cases:
            cached = self.auth.verify(route, item)
            direct = self.auth.verify_direct(route, item)
            self.assertEqual(cached.code, direct.code)
            self.assertEqual(cached.reason, direct.reason)

    def test_version_bump_wholesale_invalidates_cache(self) -> None:
        route = token_route()
        self.auth.verify(route, request())
        self.assertEqual(self.auth.cache_size(), 1)
        new_table = token_table()
        new_table.version = 2
        cleared = self.auth.replace_policy(new_table, routes_version=2)
        self.assertEqual(cleared, 1)
        self.assertEqual(self.auth.cache_size(), 0)
        result = self.auth.verify(route, request())
        self.assertFalse(result.cache_hit)

    def test_signature_verification_and_tamper_detection(self) -> None:
        route = signature_route()
        signed_at = 490
        signature = Authenticator.sign(
            token_table().signatures["key-1"],
            Request(seq=1, route="p", client_id="dave", token="key-1",
                    ts_ms=500, signed_at_ms=signed_at),
        )
        good = Request(seq=1, route="p", client_id="dave", token="key-1",
                       ts_ms=500, signed_at_ms=signed_at, signature=signature)
        self.assertTrue(self.auth.verify(route, good).ok)
        tampered = Request(seq=2, route="p", client_id="dave", token="key-1",
                           ts_ms=500, signed_at_ms=signed_at, signature="deadbeef")
        result = self.auth.verify(route, tampered)
        self.assertEqual(result.code, E_AUTH)
        self.assertEqual(result.reason, "signature_mismatch")


if __name__ == "__main__":
    unittest.main()
