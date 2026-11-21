from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from poscascade.canonical import canonical_json, object_checksum
from poscascade.errors import BusinessError, ErrorCode, ErrorCollector
from poscascade.models import load_labels, load_lexicon
from poscascade.segment import run_segmentation
from poscascade.selfcheck import run_selfcheck
from poscascade.summarize import run_summarization
from poscascade.tag import run_tagging
from poscascade.validate import read_input_lines, run_validation, validate_offsets

from .helpers import DATA, pipeline


class CoreStageTests(unittest.TestCase):
    def test_deterministic_stages_and_offsets(self) -> None:
        labels = load_labels(DATA / "labels_v1.json")
        lexicon = load_lexicon(DATA / "dict_v1.json", labels)
        from poscascade.config import load_config

        config = load_config(DATA / "config_default.json")
        collector = ErrorCollector()
        raw_rows, checksum = read_input_lines(DATA / "lines.jsonl", config, collector)
        self.assertFalse(collector.has_errors())

        validation_a = run_validation(raw_rows, checksum, config)
        validation_b = run_validation(raw_rows, checksum, config)
        segment_a = run_segmentation(validation_a, lexicon, config)
        segment_b = run_segmentation(validation_b, lexicon, config)
        self.assertEqual(canonical_json(segment_a), canonical_json(segment_b))
        self.assertEqual(
            object_checksum(segment_a["lines"]),
            object_checksum(segment_b["lines"]),
        )
        for line, validation_line in zip(segment_a["lines"], validation_a["lines"]):
            validate_offsets(
                validation_line["text"],
                line["tokens"],
                doc_id=line["doc_id"],
                line_no=line["line_no"],
            )
            self.assertEqual(
                line["text_codepoint_len"],
                sum(token["codepoint_len"] for token in line["tokens"]),
            )

        tag = run_tagging(segment_a, labels, config)
        summary = run_summarization(tag, labels)
        self.assertTrue(all(token["label"] in labels.labels for line in tag["lines"] for token in line["tokens"]))
        selfcheck = run_selfcheck(validation_a, segment_a, tag, summary, labels)
        self.assertEqual(selfcheck["code"], ErrorCode.OK)

    def test_path_tie_break_is_independent_of_dictionary_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            labels_path = root / "labels.json"
            labels_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "labels": ["n", "v", "x"],
                        "priors": {"n": 1.0, "v": 1.0, "x": 0.1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            entries = [
                {"word": "B", "freq": 1, "pos": ["v"]},
                {"word": "AB", "freq": 10, "pos": ["v", "n"]},
                {"word": "A", "freq": 10, "pos": ["v"]},
            ]
            dict_path = root / "dict.json"
            dict_path.write_text(
                json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            labels = load_labels(labels_path)
            lexicon = load_lexicon(dict_path, labels)
            from poscascade.config import PipelineConfig

            config = PipelineConfig(max_candidate_edges_per_stage=1000)
            validation = {
                "lines": [
                    {
                        "doc_id": "T1",
                        "line_no": 1,
                        "text": "AB",
                        "punct_class": "none",
                        "codepoints": ["A", "B"],
                        "codepoint_len": 2,
                        "text_sha256": "x",
                    }
                ]
            }
            segment = run_segmentation(validation, lexicon, config)
            line = segment["lines"][0]
            self.assertEqual([token["word"] for token in line["tokens"]], ["AB"])
            self.assertEqual(line["path_score"], 2.302585092994)

            tag = run_tagging(segment, labels, config)
            token = tag["lines"][0]["tokens"][0]
            self.assertEqual(token["label"], "n")
            self.assertEqual(token["scores"]["n"], token["scores"]["v"])

    def test_selfcheck_detects_inconsistencies(self) -> None:
        labels = load_labels(DATA / "labels_v1.json")
        validation = {
            "lines": [
                {
                    "doc_id": "D1",
                    "line_no": 1,
                    "text": "AB",
                    "text_sha256": "x",
                }
            ]
        }
        segment = {
            "lines": [
                {
                    "doc_id": "D1",
                    "line_no": 1,
                    "text_codepoint_len": 2,
                    "tokens": [
                        {"word": "A", "start": 0, "end": 2},
                    ],
                    "path_score": 1.0,
                }
            ]
        }
        tag = {
            "lines": [
                {
                    "doc_id": "D1",
                    "line_no": 1,
                    "token_count": 1,
                    "tokens": [
                        {"word": "A", "start": 0, "end": 1, "label": "z"},
                    ],
                }
            ]
        }
        summary = {
            "token_count": 2,
            "label_counts": {"n": 1},
            "lines": [],
        }
        report = run_selfcheck(
            validation,
            segment,
            tag,
            summary,
            labels,
            affected={"validate": ["D1"], "segment": ["D1"], "tag": ["D1"], "summarize": ["D1"]},
            recomputed={"validate": ["D1"], "segment": ["D1"], "tag": ["D1"], "summarize": []},
            reused={"validate": [], "segment": [], "tag": [], "summarize": ["D1"]},
        )
        self.assertEqual(report["code"], ErrorCode.E_INCONSISTENT)
        categories = {issue["category"] for issue in report["issues"]}
        self.assertIn("OFFSET_LEN", categories)
        self.assertIn("LABEL_NOT_CONTROLLED", categories)
        self.assertIn("SUMMARY_TOKEN_COUNT", categories)
        self.assertIn("AFFECTED_RECOMPUTE_MISMATCH", categories)

    def test_two_complete_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = pipeline(root, "first").run()
            second = pipeline(root, "second").run()
            self.assertEqual(first.code, ErrorCode.OK)
            self.assertEqual(second.code, ErrorCode.OK)
            for filename in ("annotations.jsonl", "summary.json", "segmentation.jsonl"):
                self.assertEqual(
                    (root / "first" / "out" / filename).read_bytes(),
                    (root / "second" / "out" / filename).read_bytes(),
                )

    def test_offset_error_has_doc_and_line(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            validate_offsets(
                "AB",
                [{"word": "A", "start": 0, "end": 2}],
                doc_id="D9",
                line_no=3,
            )
        self.assertEqual(raised.exception.code, ErrorCode.E_OFFSET_LEN)
        self.assertEqual(raised.exception.doc_id, "D9")
        self.assertEqual(raised.exception.line_no, 3)
