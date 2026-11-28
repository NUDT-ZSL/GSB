"""End-to-end deterministic workbench pipeline and artifact writer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .exercises import ExerciseGenerator
from .model import MaskedDenoisingScorer
from .plot import PlotChecker
from .readability import IndependentReadabilityChecker
from .rewrite import ConstrainedRewriter
from .text import words


SCHEMA_VERSION = 1
PIPELINE_VERSION = "graded-story-pipeline-v1"


def _short_hash(value: Any) -> str:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=True
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _rubric_ref_index(levels: list[dict[str, Any]]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for level in levels:
        refs = set(level["constraint_refs"].values())
        refs.add(level["modifier_policy"]["rule_ref"])
        refs.add(level["split_policy"]["rule_ref"])
        for entry in level["synonyms"]:
            for option in entry["options"]:
                refs.add(option["entry_ref"])
                refs.add(option["target_ref"])
        index[level["level_id"]] = refs
    return index


def build_pipeline(data: dict[str, Any]) -> dict[str, Any]:
    levels = sorted(data["levels"], key=lambda item: item["level_id"])
    stories = sorted(data["stories"], key=lambda item: item["story_id"])
    entity_aliases = data.get("entity_aliases", {})
    scorer = MaskedDenoisingScorer(data["model_corpus"], data["lexicon"])
    readability = IndependentReadabilityChecker(levels)
    plot_checker = PlotChecker(entity_aliases)
    exercise_generator = ExerciseGenerator(
        data["lexicon"], scorer, entity_aliases
    )

    ledger_rows: list[dict[str, Any]] = []
    plot_reviews: list[dict[str, Any]] = []
    readability_checks: list[dict[str, Any]] = []
    cloze_rows: list[dict[str, Any]] = []
    wrong_word_rows: list[dict[str, Any]] = []
    exercise_rows: list[dict[str, Any]] = []
    exercise_skips: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    story_bundles: list[dict[str, Any]] = []
    unit_count = 0
    approved_unit_count = 0

    for story in stories:
        story_bundle = {
            "schema_version": SCHEMA_VERSION,
            "story_id": story["story_id"],
            "title": story["title"],
            "exercise_seed": story["exercise_seed"],
            "critical_entities": story["critical_entities"],
            "levels": [],
        }
        for level in levels:
            level_id = level["level_id"]
            rewriter = ConstrainedRewriter(
                level, data["lexicon"], scorer, entity_aliases
            )
            units: list[dict[str, Any]] = []
            rewritten_by_sentence: dict[str, str] = {}
            for sentence in story["sentences"]:
                sentence_id = sentence["sentence_id"]
                unit_id = f"{story['story_id']}-{level_id}-{sentence_id}"
                rewrite = rewriter.rewrite(
                    sentence["text"], sentence_id, sentence["key_entities"]
                )
                rewritten_by_sentence[sentence_id] = rewrite["rewritten"]
                readability_check = readability.check(
                    rewrite["rewritten"],
                    level_id,
                    story_id=story["story_id"],
                    sentence_id=sentence_id,
                    unit_id=unit_id,
                )
                readability_checks.append(readability_check)
                units.append(
                    {
                        "unit_id": unit_id,
                        "story_id": story["story_id"],
                        "story_title": story["title"],
                        "level_id": level_id,
                        "level_name": level["name"],
                        "sentence_id": sentence_id,
                        "event_id": sentence["event_id"],
                        "event_order": sentence["event_order"],
                        "key_entities": sentence["key_entities"],
                        "original": rewrite["original"],
                        "rewritten": rewrite["rewritten"],
                        "rewrite_status": rewrite["status"],
                        "selected_candidate_id": rewrite["selected_candidate_id"],
                        "changes": rewrite["changes"],
                        "decoding": rewrite["decoding"],
                        "constraint_check": rewrite["constraints"],
                        "masked_denoising": rewrite["masked_denoising"],
                        "candidate_pool": rewrite["candidate_pool"],
                        "readability_check": readability_check,
                        "plot_check": None,
                        "approved": False,
                        "exercise_ids": [],
                    }
                )
            plot_review = plot_checker.check(
                story, level_id, rewritten_by_sentence
            )
            plot_reviews.append(plot_review)
            plot_by_sentence = {
                item["sentence_id"]: item for item in plot_review["units"]
            }
            level_exercises: list[dict[str, Any]] = []
            for unit in units:
                unit_count += 1
                plot_unit = plot_by_sentence[unit["sentence_id"]]
                unit["plot_check"] = plot_unit
                unit["approved"] = bool(
                    not plot_unit["review_required"]
                    and unit["readability_check"]["passed"]
                )
                if unit["approved"]:
                    approved_unit_count += 1
                    generated = exercise_generator.generate_for_unit(
                        story_id=story["story_id"],
                        sentence_id=unit["sentence_id"],
                        level_id=level_id,
                        unit_id=unit["unit_id"],
                        rewritten=unit["rewritten"],
                        key_entities=unit["key_entities"],
                        allowed_words=level["allowed_words"],
                        seed=int(story["exercise_seed"]),
                    )
                    for exercise in generated:
                        if exercise["status"] == "generated":
                            level_exercises.append(exercise)
                            exercise_rows.append(exercise)
                            cloze_rows.append(exercise["cloze"])
                            wrong_word_rows.append(exercise["wrong_word"])
                            unit["exercise_ids"].append(exercise["exercise_id"])
                        else:
                            exercise_skips.append(exercise)
                changes = unit["changes"]
                deleted_words = [
                    {
                        "word": change["source"],
                        "original_word_ordinal": change["original_word_ordinal"],
                        "rule_ref": change["rule_ref"],
                    }
                    for change in changes
                    if change["type"] == "delete"
                ]
                replaced_words = [
                    {
                        "from": change["source"],
                        "to": change["replacement"],
                        "original_word_ordinal": change["original_word_ordinal"],
                        "rule_ref": change["rule_ref"],
                    }
                    for change in changes
                    if change["type"] == "replace"
                ]
                split_operations = [
                    {
                        "conjunction": change["source"],
                        "original_word_ordinal": change["original_word_ordinal"],
                        "rule_ref": change["rule_ref"],
                    }
                    for change in changes
                    if change["type"] == "split"
                ]
                rubric_refs = sorted(
                    {
                        ref
                        for change in changes
                        for ref in change["rubric_refs"]
                    }
                )
                ledger = {
                    "schema_version": SCHEMA_VERSION,
                    "ledger_id": unit["unit_id"] + "-L",
                    "story_id": unit["story_id"],
                    "story_title": unit["story_title"],
                    "level_id": unit["level_id"],
                    "sentence_id": unit["sentence_id"],
                    "event_id": unit["event_id"],
                    "event_order": unit["event_order"],
                    "key_entities": unit["key_entities"],
                    "original_sentence": unit["original"],
                    "rewritten_sentence": unit["rewritten"],
                    "deleted_words": deleted_words,
                    "replaced_words": replaced_words,
                    "split_operations": split_operations,
                    "replacement_basis": changes,
                    "rubric_refs": rubric_refs,
                    "decoding": unit["decoding"],
                    "selected_candidate_id": unit["selected_candidate_id"],
                    "constraint_check": unit["constraint_check"],
                    "masked_denoising": unit["masked_denoising"],
                    "rewrite_status": unit["rewrite_status"],
                    "plot_check": {
                        "entities_ok": plot_unit["entities_ok"],
                        "event_order_ok": plot_unit["event_order_ok"],
                        "lost_entities": plot_unit["lost_entities"],
                        "review_required": plot_unit["review_required"],
                        "review_reasons": plot_unit["review_reasons"],
                    },
                    "readability_check": {
                        "passed": unit["readability_check"]["passed"],
                        "violations": unit["readability_check"]["violations"],
                    },
                    "approved": unit["approved"],
                    "exercise_ids": list(unit["exercise_ids"]),
                }
                ledger_rows.append(ledger)
                candidate_rows.append(
                    {
                        "story_id": unit["story_id"],
                        "level_id": unit["level_id"],
                        "sentence_id": unit["sentence_id"],
                        "unit_id": unit["unit_id"],
                        "selected_candidate_id": unit["selected_candidate_id"],
                        "selected": {
                            "text": unit["rewritten"],
                            "average_log_probability": unit["masked_denoising"][
                                "average_log_probability"
                            ],
                            "average_probability": unit["masked_denoising"][
                                "average_probability"
                            ],
                            "perplexity": unit["masked_denoising"]["perplexity"],
                            "positions": unit["masked_denoising"]["positions"],
                        },
                        "candidate_pool": unit["candidate_pool"],
                    }
                )
            story_bundle["levels"].append(
                {
                    "level_id": level_id,
                    "level_name": level["name"],
                    "max_words": level["max_words"],
                    "units": units,
                    "plot_review": plot_review,
                    "exercises": level_exercises,
                }
            )
        story_bundles.append(story_bundle)

    readability_report = readability.report(readability_checks)
    pairing_complete = (
        len(cloze_rows) == len(wrong_word_rows) == len(exercise_rows)
        and all(
            row["cloze"]["cloze_id"] == row["pairing"]["cloze_id"]
            and row["wrong_word"]["wrong_word_id"] == row["pairing"]["wrong_word_id"]
            for row in exercise_rows
        )
    )
    status_counts: dict[str, int] = {}
    for row in ledger_rows:
        status = row["rewrite_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    summary = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "generator": data["generator"],
        "story_count": len(stories),
        "level_count": len(levels),
        "unit_count": unit_count,
        "approved_unit_count": approved_unit_count,
        "rewrite_status_counts": dict(sorted(status_counts.items())),
        "plot": {
            "review_required_unit_count": sum(
                review["review_required_count"] for review in plot_reviews
            ),
            "lost_entity_count": sum(
                review["lost_entity_count"] for review in plot_reviews
            ),
            "event_order_violation_count": sum(
                review["event_order_violation_count"] for review in plot_reviews
            ),
            "passed": all(review["passed"] for review in plot_reviews),
        },
        "readability": {
            "checked_unit_count": readability_report["checked_unit_count"],
            "passed_unit_count": readability_report["passed_unit_count"],
            "violating_unit_count": readability_report["violating_unit_count"],
            "violation_count": readability_report["violation_count"],
            "passed": readability_report["passed"],
        },
        "exercises": {
            "generated_exercise_count": len(exercise_rows),
            "cloze_count": len(cloze_rows),
            "wrong_word_count": len(wrong_word_rows),
            "skipped_exercise_count": len(exercise_skips),
            "pairing_complete": pairing_complete,
        },
        "model": scorer.describe(),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "ledger": ledger_rows,
        "plot_reviews": plot_reviews,
        "readability_checks": readability_checks,
        "cloze": cloze_rows,
        "wrong_word": wrong_word_rows,
        "exercises": exercise_rows,
        "exercise_skips": exercise_skips,
        "scored_candidates": candidate_rows,
        "story_bundles": story_bundles,
    }
    payload_sha256 = _short_hash(payload)
    payload.update(
        {
            "summary": {**summary, "payload_sha256": payload_sha256},
            "readability_report": readability_report,
            "plot_review_report": {
                "story_count": len(stories),
                "level_review_count": len(plot_reviews),
                "review_required_unit_count": summary["plot"][
                    "review_required_unit_count"
                ],
                "reviews": plot_reviews,
                "passed": summary["plot"]["passed"],
            },
        }
    )
    return payload


def validate_pipeline(data: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    rubric_refs = _rubric_ref_index(data["levels"])
    normalized_aliases = {
        key.lower(): [item.lower() for item in values]
        for key, values in data.get("entity_aliases", {}).items()
    }
    errors: list[str] = []
    referenced_refs = set()
    for row in run["ledger"]:
        changed_ordinals = [
            change["original_word_ordinal"]
            for change in row["replacement_basis"]
            if change["type"] in {"replace", "delete"}
        ]
        if len(changed_ordinals) != len(set(changed_ordinals)):
            errors.append(
                f"multiple lexical changes target one word in {row['ledger_id']}"
            )
        for change in row["replacement_basis"]:
            referenced_refs.update(change["rubric_refs"])
            if change["rule_ref"] not in rubric_refs[row["level_id"]]:
                errors.append(
                    f"unknown rubric reference {change['rule_ref']} in {row['ledger_id']}"
                )
    for ref in referenced_refs:
        if not any(ref in level_refs for level_refs in rubric_refs.values()):
            errors.append(f"orphan rubric reference {ref}")

    cloze_by_id = {row["cloze_id"]: row for row in run["cloze"]}
    wrong_by_id = {row["wrong_word_id"]: row for row in run["wrong_word"]}
    if len(cloze_by_id) != len(run["cloze"]):
        errors.append("duplicate cloze identifiers")
    if len(wrong_by_id) != len(run["wrong_word"]):
        errors.append("duplicate wrong-word identifiers")
    if len(run["cloze"]) != len(run["wrong_word"]):
        errors.append("cloze and wrong-word counts differ")
    exercise_ids = [exercise["exercise_id"] for exercise in run["exercises"]]
    if len(exercise_ids) != len(set(exercise_ids)):
        errors.append("duplicate exercise identifiers")
    for exercise in run["exercises"]:
        cloze = exercise["cloze"]
        wrong = exercise["wrong_word"]
        if cloze["cloze_id"] not in cloze_by_id:
            errors.append(f"missing exported cloze for {exercise['exercise_id']}")
        if wrong["wrong_word_id"] not in wrong_by_id:
            errors.append(f"missing exported wrong word for {exercise['exercise_id']}")
        if cloze["not_key_entity"] is not True:
            errors.append(f"cloze targets an entity in {exercise['exercise_id']}")
        surfaces = {
            entity.lower()
            for entity in exercise["target_key_entities"]
        }
        for entity in list(surfaces):
            surfaces.update(
                alias.lower()
                for alias in normalized_aliases.get(entity, [])
            )
        if cloze["answer"].lower() in surfaces:
            errors.append(f"cloze answer is a key entity in {exercise['exercise_id']}")
        if wrong["target_pos"] != wrong["replacement_pos"]:
            errors.append(f"wrong word changes POS in {exercise['exercise_id']}")
        if not wrong["frame_match"]:
            errors.append(f"wrong word changes verb frame in {exercise['exercise_id']}")
        if not wrong["syntax_position_legal"]:
            errors.append(f"wrong word is not position-legal in {exercise['exercise_id']}")
        if not wrong["context_inappropriate"]:
            errors.append(
                f"wrong word is not context-inappropriate in {exercise['exercise_id']}"
            )
        if exercise["pairing"]["shared_position"] != cloze["position"]:
            errors.append(f"pairing position mismatch in {exercise['exercise_id']}")
        if cloze["sentence"].count("___") != 1:
            errors.append(f"cloze does not contain exactly one blank in {exercise['exercise_id']}")
        position = wrong["position"]
        fragment_ordinal = position["fragment_word_ordinal"]
        original_words = words(wrong["original_sentence"])
        perturbed_words = words(wrong["perturbed_sentence"])
        if fragment_ordinal < 1 or fragment_ordinal > len(original_words):
            errors.append(f"invalid exercise position in {exercise['exercise_id']}")
        else:
            if original_words[fragment_ordinal - 1].lower() != cloze["answer"].lower():
                errors.append(f"cloze answer does not match position in {exercise['exercise_id']}")
            if perturbed_words[fragment_ordinal - 1].lower() != wrong["replacement_word"].lower():
                errors.append(f"wrong word does not match reported position in {exercise['exercise_id']}")

    approved_units = [
        unit
        for bundle in run["story_bundles"]
        for level in bundle["levels"]
        for unit in level["units"]
        if unit["approved"]
    ]
    units_without_exercises = [
        unit["unit_id"] for unit in approved_units if not unit["exercise_ids"]
    ]
    if units_without_exercises:
        errors.append(
            "approved units without exercises: " + ", ".join(units_without_exercises)
        )
    if run["exercise_skips"]:
        errors.append(
            f"{len(run['exercise_skips'])} exercise generation attempts were skipped"
        )

    summary = run["summary"]
    if not summary["readability"]["passed"]:
        errors.append(
            f"independent readability checker found {summary['readability']['violation_count']} violations"
        )
    if not summary["plot"]["passed"]:
        errors.append(
            f"plot checker found {summary['plot']['review_required_unit_count']} units needing review"
        )
    if not summary["exercises"]["pairing_complete"]:
        errors.append("cloze/wrong-word pairing is incomplete")

    return {
        "checker": "pipeline-acceptance-v1",
        "checks": {
            "rubric_references_resolve": not any(
                "rubric reference" in error or "orphan" in error for error in errors
            ),
            "one_change_per_word_position": not any(
                "multiple lexical changes" in error for error in errors
            ),
            "cloze_and_wrong_word_pair": (
                len(run["cloze"]) == len(run["wrong_word"]) == len(run["exercises"])
                and len(exercise_ids) == len(set(exercise_ids))
            ),
            "cloze_avoids_key_entities": not any(
                "cloze" in error and "entit" in error for error in errors
            ),
            "wrong_words_are_same_pos_and_context_inappropriate": not any(
                "wrong word" in error for error in errors
            ),
            "all_approved_units_have_exercises": not units_without_exercises,
            "independent_readability_passed": summary["readability"]["passed"],
            "plot_coverage_passed": summary["plot"]["passed"],
        },
        "errors": errors,
        "passed": not errors,
    }


def verify_determinism(data: dict[str, Any]) -> dict[str, Any]:
    first = build_pipeline(data)
    second = build_pipeline(data)
    first_hash = first["summary"]["payload_sha256"]
    second_hash = second["summary"]["payload_sha256"]
    validation = validate_pipeline(data, first)
    return {
        "checker": "repeatability-v1",
        "runs": 2,
        "first_payload_sha256": first_hash,
        "second_payload_sha256": second_hash,
        "identical_payloads": first_hash == second_hash,
        "pipeline_validation": validation,
        "passed": first_hash == second_hash and validation["passed"],
    }


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=True,
        )
        + "\n"
    ).encode("utf-8")


def canonical_jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=True,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")


def write_pipeline_outputs(
    run: dict[str, Any],
    out_dir: str | Path,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    old_manifest = out_dir / "manifest.json"
    if old_manifest.is_file():
        try:
            old_data = json.loads(old_manifest.read_text(encoding="utf-8"))
            for entry in old_data.get("files", []):
                relative = entry.get("path", "")
                old_path = (out_dir / relative).resolve()
                try:
                    old_path.relative_to(out_dir.resolve())
                except ValueError:
                    continue
                if old_path.is_file():
                    old_path.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if verification is None:
        stale_verification = out_dir / "verification.json"
        if stale_verification.is_file():
            stale_verification.unlink()
    files: dict[str, bytes] = {
        "summary.json": canonical_json_bytes(run["summary"]),
        "ledger.jsonl": canonical_jsonl_bytes(run["ledger"]),
        "plot_review.json": canonical_json_bytes(run["plot_review_report"]),
        "readability_report.json": canonical_json_bytes(run["readability_report"]),
        "cloze.jsonl": canonical_jsonl_bytes(run["cloze"]),
        "wrong_word.jsonl": canonical_jsonl_bytes(run["wrong_word"]),
        "exercises.jsonl": canonical_jsonl_bytes(run["exercises"]),
        "exercise_skips.jsonl": canonical_jsonl_bytes(run["exercise_skips"]),
        "scored_candidates.jsonl": canonical_jsonl_bytes(run["scored_candidates"]),
    }
    for bundle in run["story_bundles"]:
        files[f"stories/{bundle['story_id']}.json"] = canonical_json_bytes(bundle)
    if verification is not None:
        files["verification.json"] = canonical_json_bytes(verification)
    hashes = {
        relative: hashlib.sha256(content).hexdigest()
        for relative, content in sorted(files.items())
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "payload_sha256": run["summary"]["payload_sha256"],
        "files": [
            {
                "path": relative,
                "sha256": digest,
                "bytes": len(files[relative]),
            }
            for relative, digest in sorted(hashes.items())
        ],
        "counts": {
            "stories": run["summary"]["story_count"],
            "levels": run["summary"]["level_count"],
            "units": run["summary"]["unit_count"],
            "ledger_rows": len(run["ledger"]),
            "cloze": len(run["cloze"]),
            "wrong_word": len(run["wrong_word"]),
            "exercises": len(run["exercises"]),
        },
    }
    files["manifest.json"] = canonical_json_bytes(manifest)
    for relative, content in sorted(files.items()):
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return manifest
