"""Deterministic normalization, preanalysis, arc parsing and root checks."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .common import canonical_hash
from .errors import Codes, DomainError
from .rules import PARSER_CONFIG_VERSION


def normalize_sentence(task: dict) -> dict:
    sid = task["sentence_id"]
    tokens = task["token_list"]
    poses = task["pos_tag_list"]
    if not isinstance(tokens, list) or not all(isinstance(x, str) and x for x in tokens):
        raise DomainError(
            "token_list must be a non-empty string array",
            code=Codes.E_TOKEN_VALIDATION,
            sentence_id=sid,
        )
    if not isinstance(poses, list) or len(poses) != len(tokens):
        raise DomainError(
            "pos_tag_list length must equal token_list length",
            code=Codes.E_TOKEN_VALIDATION,
            sentence_id=sid,
            token_count=len(tokens) if isinstance(tokens, list) else None,
            pos_count=len(poses) if isinstance(poses, list) else None,
        )

    normalized_raw = unicodedata.normalize("NFKC", str(task["raw_text"])).strip()
    normalized_raw = re.sub(r"\s+", " ", normalized_raw)
    reconstructed = " ".join(tokens)
    if normalized_raw != reconstructed:
        raise DomainError(
            "normalized raw_text does not match token_list reconstruction",
            code=Codes.E_TOKEN_VALIDATION,
            sentence_id=sid,
            normalized_raw=normalized_raw,
            reconstructed=reconstructed,
        )

    token_hash = canonical_hash({"token_list": tokens})
    indexed = [
        {"index": index, "text": token, "pos": poses[index - 1]}
        for index, token in enumerate(tokens, 1)
    ]
    return {
        "node": "normalize",
        "sentence_id": sid,
        "lang": task.get("lang"),
        "normalized_text": normalized_raw,
        "token_sequence_hash": token_hash,
        "tokens": indexed,
        "validation": "OK",
    }


def syntax_tag_for(pos: str, ir: dict) -> str:
    tag_set = set(ir["syntax_tags"])
    upper = str(pos).upper()
    if upper in tag_set:
        return upper
    mapped = "DEP"
    if upper.startswith("NN"):
        mapped = "NOUN"
    elif upper.startswith("VB"):
        mapped = "VERB"
    elif upper.startswith("JJ"):
        mapped = "ADJ"
    elif upper.startswith("RB"):
        mapped = "ADV"
    elif upper.startswith("DT"):
        mapped = "DET"
    if mapped in tag_set:
        return mapped
    if "DEP" in tag_set:
        return "DEP"
    raise DomainError("rule syntax tag set lacks fallback tag DEP", code=Codes.E_SEMANTIC_DRIFT)


def preanalyze(norm: dict, ir: dict) -> dict:
    sid = norm["sentence_id"]
    tokens = norm["tokens"]
    count = len(tokens)
    tags = {token["index"]: syntax_tag_for(token["pos"], ir) for token in tokens}
    tag_rows = [{"token_index": i, "syntax_tag": tags[i]} for i in range(1, count + 1)]

    spans: list[dict] = []
    i = 1
    while i <= count:
        tag = tags[i]
        if tag == "PRON":
            spans.append({"span_id": f"NP:{i}:{i}", "label": "NP", "start": i, "end": i, "head": i})
            i += 1
            continue
        if tag == "DET":
            start = i
            j = i + 1
            while j <= count and tags[j] == "ADJ":
                j += 1
            if j <= count and tags[j] in {"NOUN", "PROPN"}:
                end = j
                head = j
            else:
                end = i
                head = i
            spans.append({
                "span_id": f"NP:{start}:{end}",
                "label": "NP",
                "start": start,
                "end": end,
                "head": head,
            })
            i = end + 1
            continue
        if tag == "ADJ":
            start = i
            end = i
            while end + 1 <= count and tags[end + 1] == "ADJ":
                end += 1
            if end + 1 <= count and tags[end + 1] in {"NOUN", "PROPN"}:
                end += 1
                head = end
            else:
                head = start
            spans.append({
                "span_id": f"NP:{start}:{end}",
                "label": "NP",
                "start": start,
                "end": end,
                "head": head,
            })
            i = end + 1
            continue
        if tag in {"NOUN", "PROPN"}:
            start = i
            end = i
            while end + 1 <= count and tags[end + 1] in {"NOUN", "PROPN"}:
                end += 1
            spans.append({
                "span_id": f"NP:{start}:{end}",
                "label": "NP",
                "start": start,
                "end": end,
                "head": end,
            })
            i = end + 1
            continue
        i += 1

    structural_root = next((t["index"] for t in tokens if tags[t["index"]] == "VERB"), None)
    if structural_root is None:
        structural_root = next((t["index"] for t in tokens if tags[t["index"]] == "AUX"), None)
    if structural_root is None:
        non_punct = [t["index"] for t in tokens if tags[t["index"]] != "PUNCT"]
        structural_root = non_punct[-1] if non_punct else 1

    spans.append({
        "span_id": f"S:1:{count}",
        "label": "S",
        "start": 1,
        "end": count,
        "head": structural_root,
    })
    spans.sort(key=lambda span: (span["start"], span["end"], span["label"]))
    return {
        "node": "preanalysis",
        "sentence_id": sid,
        "syntax_tags": tag_rows,
        "syntax_tree_set": spans,
        "structural_root": structural_root,
    }


def actual_direction(head: int, dependent: int) -> str:
    if head == 0:
        return "artificial_root"
    if dependent < head:
        return "dependent_before_head"
    if dependent > head:
        return "head_before_dependent"
    return "self_loop"


def parse_arcs(norm: dict, pre: dict, ir: dict) -> tuple[dict, dict]:
    sid = norm["sentence_id"]
    tokens = norm["tokens"]
    count = len(tokens)
    tags = {row["token_index"]: row["syntax_tag"] for row in pre["syntax_tags"]}
    assigned: dict[int, tuple[int, str, str]] = {}

    def assign(dependent: int, head: int, label: str) -> None:
        if dependent == head or dependent in assigned:
            return
        assigned[dependent] = (head, label, tags[dependent])

    np_spans = [span for span in pre["syntax_tree_set"] if span["label"] == "NP"]
    for span in np_spans:
        head = span["head"]
        for i in range(span["start"], span["end"] + 1):
            if i == head:
                continue
            tag = tags[i]
            if tag == "DET":
                label = "det"
            elif tag == "ADJ":
                label = "amod"
            elif tag in {"NOUN", "PROPN"}:
                label = "compound"
            else:
                label = "dep"
            assign(i, head, label)

    for i in range(1, count + 1):
        if tags[i] == "ADP":
            target = next(
                (span["head"] for span in np_spans
                 if span["start"] > i and span["start"] <= i + 2),
                None,
            )
            if target is None:
                target = next(
                    (j for j in range(i + 1, min(count, i + 2) + 1)
                     if tags[j] in {"NOUN", "PROPN"}),
                    None,
                )
            if target:
                assign(i, target, "case")

    verbs = [i for i in range(1, count + 1) if tags[i] == "VERB"]
    if not verbs:
        verbs = [i for i in range(1, count + 1) if tags[i] == "AUX"]
    for verb in verbs:
        for i in range(1, verb):
            if tags[i] in {"AUX", "ADV"}:
                assign(i, verb, "aux" if tags[i] == "AUX" else "advmod")

    for verb in verbs:
        candidates = [span["head"] for span in np_spans if span["end"] < verb]
        subject = max(candidates) if candidates else None
        if subject is None:
            before = [i for i in range(1, verb) if tags[i] in {"PRON", "NOUN", "PROPN"}]
            subject = before[-1] if before else None
        if subject is not None:
            assign(subject, verb, "nsubj")

        object_head = None
        for span in sorted(np_spans, key=lambda item: item["start"]):
            if span["start"] <= verb or span["start"] > verb + 3:
                continue
            if any(tags[i] == "ADP" for i in range(verb + 1, span["start"])):
                continue
            object_head = span["head"]
            break
        if object_head is not None:
            assign(object_head, verb, "obj")

        for span in np_spans:
            head = span["head"]
            if head <= verb or head in assigned:
                continue
            has_adp = any(tags[i] == "ADP" for i in range(verb + 1, span["start"]))
            assign(head, verb, "obl" if has_adp or tags[head] == "NOUN" else "dep")

    structural_root = pre["structural_root"]
    for i in range(1, count + 1):
        if tags[i] == "PUNCT":
            assign(i, structural_root, "punct")
    for i in range(1, count + 1):
        if i != structural_root and i not in assigned:
            assign(i, structural_root, "dep")

    arcs: list[dict] = []
    labels: list[dict] = []
    for dependent in sorted(assigned):
        head, label, source_tag = assigned[dependent]
        arc = {
            "arc_id": f"arc:h{head}:d{dependent}",
            "head": head,
            "dependent": dependent,
            "label": label,
            "direction": actual_direction(head, dependent),
            "source_syntax_tag": source_tag,
            "head_syntax_tag": tags[head],
        }
        arcs.append(arc)
        labels.append({
            "arc_id": arc["arc_id"],
            "token_index": dependent,
            "label": label,
            "source_syntax_tag": source_tag,
        })

    return (
        {"node": "arcs", "sentence_id": sid, "structural_root": structural_root, "arcs": arcs},
        {"node": "labels", "sentence_id": sid, "labels": labels},
    )


def select_root(norm: dict, pre: dict, ir: dict) -> dict:
    rp = ir["root_policy"]
    tokens = norm["tokens"]
    count = len(tokens)
    window = max(1, min(int(rp.get("search_window", count)), count))
    selected = None
    selection_path: list[dict] = []
    strategy = rp.get("strategy", "pos_priority_first")

    if strategy == "fixed_token":
        candidate = int(rp.get("token_index", 1))
        if 1 <= candidate <= count:
            selected = candidate
            selection_path.append({"step": "fixed_token", "index": selected})
    else:
        for pos in rp.get("pos_priority", ["VERB", "AUX"]):
            match = next((t["index"] for t in tokens[:window] if t["pos"] == pos), None)
            selection_path.append({"step": "pos_priority", "pos": pos, "index": match})
            if match is not None:
                selected = match
                break

    fallback_used = False
    if selected is None:
        fallback = rp.get("fallback") or (rp.get("fallback_chain") or [{}])[0]
        strategy_name = fallback.get("strategy", "preanalysis_head")
        fallback_used = True
        if strategy_name == "first_content":
            selected = next((t["index"] for t in tokens if t["pos"] != "PUNCT"), 1)
        elif strategy_name == "rightmost_content":
            selected = [t["index"] for t in tokens if t["pos"] != "PUNCT"][-1]
        else:
            selected = pre["structural_root"]
        selection_path.append({"step": "fallback", "strategy": strategy_name, "index": selected})

    tag_map = {row["token_index"]: row["syntax_tag"] for row in pre["syntax_tags"]}
    basis = {
        "strategy": strategy,
        "pos_priority": rp.get("pos_priority", []),
        "search_window": rp.get("search_window"),
        "fallback_used": fallback_used,
        "selection_path": selection_path,
        "tag_set_version": ir["tag_set_version"],
        "root_policy_version": ir["root_policy_version"],
    }
    return {
        "index": selected,
        "token": tokens[selected - 1]["text"],
        "label": rp.get("root_label", "root"),
        "source_syntax_tag": tag_map[selected],
        "decision_basis": basis,
    }


def crosscheck_and_reroot(arcs_doc: dict, root_info: dict, ir: dict) -> dict:
    sid = arcs_doc["sentence_id"]
    arcs = [dict(arc) for arc in arcs_doc["arcs"]]
    structural_root = arcs_doc["structural_root"]
    selected_root = root_info["index"]
    checks: list[dict] = []
    errors: list[dict] = []

    label_set = set(ir["dependency_labels"])
    tag_set = set(ir["syntax_tags"])
    bad_labels = sorted({arc["label"] for arc in arcs if arc["label"] not in label_set})
    checks.append({"check": "allowed_labels", "status": "OK" if not bad_labels else "ERROR", "bad": bad_labels})
    if bad_labels:
        errors.append({"category": "label_set", "labels": bad_labels})

    bad_tags = sorted({arc["source_syntax_tag"] for arc in arcs if arc["source_syntax_tag"] not in tag_set})
    checks.append({"check": "allowed_syntax_tags", "status": "OK" if not bad_tags else "ERROR", "bad": bad_tags})
    if bad_tags:
        errors.append({"category": "syntax_tag_set", "tags": bad_tags})

    parent = {arc["dependent"]: arc["head"] for arc in arcs}
    duplicate_dependents = len(parent) != len(arcs)
    checks.append({"check": "one_head_per_token", "status": "OK" if not duplicate_dependents else "ERROR"})
    if duplicate_dependents:
        errors.append({"category": "duplicate_head"})

    direction_errors = []
    for arc in arcs:
        expected = ir["direction"]["by_label"].get(arc["label"], "any")
        if expected in {"any", "artificial_root"} or expected == arc["direction"]:
            continue
        direction_errors.append({"arc_id": arc["arc_id"], "expected": expected, "actual": arc["direction"]})
    checks.append({
        "check": "direction_contract",
        "status": "OK" if not direction_errors else "ERROR",
        "errors": direction_errors,
    })
    if direction_errors:
        errors.append({"category": "direction", "arcs": direction_errors})

    cycle_starts = []
    for start in parent:
        seen = []
        x = start
        while x in parent:
            if x in seen:
                cycle_starts.append(start)
                break
            seen.append(x)
            x = parent[x]
    checks.append({"check": "acyclic", "status": "OK" if not cycle_starts else "ERROR", "starts": sorted(set(cycle_starts))})
    if cycle_starts:
        errors.append({"category": "cycle", "starts": sorted(set(cycle_starts))})

    if errors:
        raise DomainError(
            "semantic cross-check failed",
            code=Codes.E_SEMANTIC_DRIFT,
            sentence_id=sid,
            difference_category=errors[0].get("category"),
            errors=errors,
        )

    reroot_actions: list[dict] = []
    final_arcs = [dict(arc) for arc in arcs]
    if selected_root != structural_root:
        path: list[tuple[int, int]] = []
        x = selected_root
        while x != structural_root:
            parent_index = parent.get(x)
            if parent_index is None:
                raise DomainError(
                    "cannot reroot outside parsed tree",
                    code=Codes.E_SEMANTIC_DRIFT,
                    sentence_id=sid,
                    node=x,
                )
            path.append((x, parent_index))
            x = parent_index

        for child, old_head in path:
            original = next(arc for arc in final_arcs if arc["dependent"] == child and not arc.get("rerooted"))
            replacement = {
                "arc_id": original["arc_id"],
                "head": child,
                "dependent": old_head,
                "label": "dep",
                "direction": actual_direction(child, old_head),
                "source_syntax_tag": original["head_syntax_tag"],
                "head_syntax_tag": original["source_syntax_tag"],
                "rerooted": True,
                "rerooted_from_label": original["label"],
            }
            for index, arc in enumerate(final_arcs):
                if arc["arc_id"] == original["arc_id"]:
                    final_arcs[index] = replacement
                    break
            reroot_actions.append({
                "arc_id": original["arc_id"],
                "old_head": old_head,
                "new_head": child,
                "old_label": original["label"],
                "new_label": "dep",
            })

    root_arc = {
        "arc_id": f"arc:root:0:d{selected_root}",
        "head": 0,
        "dependent": selected_root,
        "label": root_info["label"],
        "direction": "artificial_root",
        "source_syntax_tag": root_info["source_syntax_tag"],
        "head_syntax_tag": "ROOT",
        "root_arc": True,
    }
    final_arcs.append(root_arc)
    final_arcs.sort(key=lambda arc: (arc["dependent"], arc["head"], arc["label"]))
    final_labels = [
        {
            "arc_id": arc["arc_id"],
            "token_index": arc["dependent"],
            "label": arc["label"],
            "source_syntax_tag": arc["source_syntax_tag"],
        }
        for arc in final_arcs
    ]
    checks.append({"check": "single_root", "status": "OK", "root": selected_root})
    checks.append({"check": "root_policy", "status": "OK", "rerooted": bool(reroot_actions)})

    return {
        "node": "root_crosscheck",
        "sentence_id": sid,
        "roots": [root_info],
        "structural_root": structural_root,
        "reroot_actions": reroot_actions,
        "root_arcs": [root_arc],
        "final_arcs": final_arcs,
        "final_labels": final_labels,
        "crosscheck": {"status": "OK", "checks": checks},
    }


def build_version_chain(task: dict, snapshot: dict, ir: dict) -> list[dict]:
    return [
        {"name": "token_sequence_hash", "value": canonical_hash({"token_list": task["token_list"]})},
        {"name": "rule_snapshot_hash", "value": snapshot["source_hash"]},
        {"name": "semantic_hash", "value": ir["semantic_hash"]},
        {"name": "tag_set_version", "value": ir["tag_set_version"]},
        {"name": "label_set_version", "value": ir["label_set_version"]},
        {"name": "root_policy_version", "value": ir["root_policy_version"]},
        {"name": "direction_version", "value": ir["direction_version"]},
        {"name": "parser_config_version", "value": PARSER_CONFIG_VERSION},
    ]


def build_export(
    task: dict,
    pre: dict,
    labels_doc: dict,
    root_doc: dict,
    hashes: dict[str, str],
    keys: dict[str, str],
    snapshot: dict,
    ir: dict,
) -> dict:
    sid = task["sentence_id"]
    arc_traces = []
    for arc in root_doc["final_arcs"]:
        producer = "root_crosscheck" if arc.get("root_arc") or arc.get("rerooted") else "arcs"
        trace = dict(arc)
        trace.update({
            "source_sentence_id": sid,
            "source_syntax_tag": arc.get("source_syntax_tag"),
            "path_node_versions": dict(sorted(hashes.items())),
            "path_node_cache_keys": dict(sorted(keys.items())),
            "cache_key": keys[producer],
            "producer_node": producer,
            "root_decision_basis": root_doc["roots"][0]["decision_basis"],
        })
        arc_traces.append(trace)

    return {
        "node": "export",
        "sentence_id": sid,
        "lang": task["lang"],
        "raw_text": task["raw_text"],
        "syntax_tree_set": pre["syntax_tree_set"],
        "dependency_arcs": arc_traces,
        "parse_labels": root_doc["final_labels"],
        "stage3_labels": labels_doc["labels"],
        "roots": root_doc["roots"],
        "constraints": {
            "constraints": ir["constraints"],
            "direction_by_label": ir["direction"]["by_label"],
            "tag_set_version": ir["tag_set_version"],
            "label_set_version": ir["label_set_version"],
            "semantic_hash": ir["semantic_hash"],
        },
        "stage_checksums": {
            "stage_1_normalize_validate": hashes["normalize"],
            "stage_2_preanalysis": hashes["preanalysis"],
            "stage_3_parse_arcs_labels": canonical_hash({
                "arcs": hashes["arcs"],
                "labels": hashes["labels"],
            }),
            "stage_4_root_regression_crosscheck": hashes["root_crosscheck"],
        },
        "cache_keys": dict(sorted(keys.items())),
        "version_chain": build_version_chain(task, snapshot, ir),
    }
