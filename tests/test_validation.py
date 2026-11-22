from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lease_arbiter.errors import Code, ValidationFailure
from lease_arbiter.samplegen import sample_documents
from lease_arbiter.validation import load_bundle

from tests.helpers import cli, load_args, write_docs


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _validate(self, documents: dict) -> list[str]:
        paths = write_docs(self.directory / "data", documents)
        with self.assertRaises(ValidationFailure) as context:
            load_bundle(
                paths["accelerator_cards.json"],
                paths["jobs.json"],
                paths["lease_config.json"],
                paths["anomaly_injections.json"],
            )
        return [item.code for item in context.exception.errors]

    def test_duplicate_ids_have_independent_codes(self) -> None:
        documents = copy.deepcopy(sample_documents())
        documents["accelerator_cards.json"]["cards"].append(
            copy.deepcopy(documents["accelerator_cards.json"]["cards"][0])
        )
        codes = self._validate(documents)
        self.assertIn(Code.CARD_DUPLICATE, codes)

        documents = copy.deepcopy(sample_documents())
        documents["jobs.json"]["jobs"].append(
            copy.deepcopy(documents["jobs.json"]["jobs"][0])
        )
        codes = self._validate(documents)
        self.assertIn(Code.JOB_DUPLICATE, codes)

    def test_card_positive_and_divisibility_codes(self) -> None:
        documents = copy.deepcopy(sample_documents())
        documents["accelerator_cards.json"]["cards"][0][
            "total_compute_shares"
        ] = 0
        codes = self._validate(documents)
        self.assertIn(Code.CARD_COMPUTE, codes)

        documents = copy.deepcopy(sample_documents())
        documents["accelerator_cards.json"]["cards"][0][
            "total_memory_shares"
        ] = 10
        codes = self._validate(documents)
        self.assertIn(Code.CARD_DIVISIBILITY, codes)

    def test_job_fields_and_grace_codes(self) -> None:
        cases = [
            ("compute_shares", 0, Code.JOB_DEMAND),
            ("lease_duration_ms", 0, Code.JOB_DURATION),
            ("priority", "urgent", Code.JOB_PRIORITY),
            ("quota_group", "missing", Code.JOB_QUOTA_GROUP),
            ("lease_duration_ms", 3000, Code.GRACE_NOT_LESS_THAN_DURATION),
        ]
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                documents = copy.deepcopy(sample_documents())
                documents["jobs.json"]["jobs"][0][field] = value
                codes = self._validate(documents)
                self.assertIn(expected, codes)

    def test_non_cross_job_must_fit_one_card(self) -> None:
        documents = copy.deepcopy(sample_documents())
        documents["jobs.json"]["jobs"].append(
            {
                "job_id": "job-too-large",
                "compute_shares": 200,
                "memory_shares": 8,
                "lease_duration_ms": 10000,
                "priority": "normal",
                "quota_group": "batch",
                "preemptible": True,
                "allow_cross_card": False,
                "arrival_round": 1,
            }
        )
        codes = self._validate(documents)
        self.assertIn(Code.JOB_SINGLE_CARD, codes)

    def test_anomaly_contract_is_closed(self) -> None:
        documents = copy.deepcopy(sample_documents())
        documents["anomaly_injections.json"]["injections"][0]["stage"] = "bogus"
        codes = self._validate(documents)
        self.assertIn(Code.ANOMALY_ENTRY, codes)

        documents = copy.deepcopy(sample_documents())
        documents["anomaly_injections.json"]["injections"][0][
            "type"
        ] = "fragment_digest_mismatch"
        codes = self._validate(documents)
        self.assertIn(Code.ANOMALY_ENTRY, codes)

    def test_invalid_load_writes_no_state(self) -> None:
        documents = copy.deepcopy(sample_documents())
        documents["jobs.json"]["jobs"][0]["priority"] = "invalid"
        paths = write_docs(self.directory / "bad-data", documents)
        db = self.directory / "state" / "ledger.db"
        pointer = self.directory / "state" / "pointer.json"
        reports = self.directory / "state" / "reports"
        completed = cli(
            *load_args(paths),
            db=db,
            pointer=pointer,
            reports=reports,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(db.exists())
        self.assertFalse(pointer.exists())
        self.assertFalse(reports.exists())


if __name__ == "__main__":
    unittest.main()
