from __future__ import annotations

import hashlib
import unittest

from lbdiag.errors import (
    E_CONN_REFUSED,
    E_MALFORMED_RESPONSE,
    E_SAMPLE_MISMATCH,
    E_STATUS_MISMATCH,
    E_TIMEOUT,
)
from lbdiag.model import Address, InstanceConfig, ScheduledCheck
from lbdiag.probe import probe_instance

from .helpers import RunningServer, free_port


def instance_for(port: int, path: str, expected_body: str | None = None) -> InstanceConfig:
    return InstanceConfig(
        instance_id="probe",
        address=Address("http", "127.0.0.1", port, path),
        weight=1,
        expected_status=200,
        expected_body_sha256=(
            hashlib.sha256(expected_body.encode()).hexdigest()
            if expected_body is not None
            else None
        ),
        failure_threshold=1,
        success_threshold=1,
        raw={},
    )


class ProbeTests(unittest.TestCase):
    def test_real_response_classes(self) -> None:
        routes = {
            "/ok": {"status": 200, "body": "ok"},
            "/slow": {"status": 200, "body": "slow", "sleep_ms": 300},
            "/bad": {"status": 503, "body": "bad"},
            "/malformed": {"malformed": True},
            "/wrong-body": {"status": 200, "body": "actual"},
            "/empty": {"status": 200, "body": ""},
        }
        with RunningServer(routes) as server:
            scheduled = ScheduledCheck("probe", 0, 1000, 0)
            success = probe_instance(instance_for(server.port, "/ok", "ok"), scheduled, 100)
            self.assertEqual(success["error_code"], "E_OK")
            self.assertFalse(success["abort_requested"])

            mismatch = probe_instance(
                instance_for(server.port, "/bad", "bad"), scheduled, 100
            )
            self.assertEqual(mismatch["error_code"], E_STATUS_MISMATCH)

            timeout = probe_instance(
                instance_for(server.port, "/slow", "slow"), scheduled, 60
            )
            self.assertEqual(timeout["error_code"], E_TIMEOUT)
            self.assertTrue(timeout["abort_requested"])
            self.assertGreaterEqual(timeout["measured_elapsed_us"], 50_000)

            malformed = probe_instance(
                instance_for(server.port, "/malformed"), scheduled, 100
            )
            self.assertEqual(malformed["error_code"], E_MALFORMED_RESPONSE)

            body_mismatch = probe_instance(
                instance_for(server.port, "/wrong-body", "expected"), scheduled, 100
            )
            self.assertEqual(body_mismatch["error_code"], E_SAMPLE_MISMATCH)

            empty_success = probe_instance(
                instance_for(server.port, "/empty", ""), scheduled, 100
            )
            self.assertEqual(empty_success["error_code"], "E_OK")
            self.assertEqual(
                empty_success["body_sha256"],
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )

            refused_port = free_port()
            refused = probe_instance(
                instance_for(refused_port, "/none"), scheduled, 100
            )
            self.assertEqual(refused["error_code"], E_CONN_REFUSED)


if __name__ == "__main__":
    unittest.main()

