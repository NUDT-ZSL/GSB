from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, canonical_bytes, read_json, text_hash


SCENARIOS: list[dict[str, Any]] = [
    {
        "doc_id": "D01",
        "source": "provincial_emergency",
        "events": [
            {
                "local_id": "E01",
                "event_type": "typhoon_landfall",
                "hazard": "H-HY",
                "location": "L-LH",
                "organization": "O-PROV",
                "time": "T-0721",
                "trigger_index": 0,
                "template_index": 0,
            }
        ],
    },
    {
        "doc_id": "D02",
        "source": "local_daily",
        "events": [
            {
                "local_id": "E01",
                "event_type": "typhoon_landfall",
                "hazard": "H-HY",
                "location": "L-LH",
                "organization": "O-LH",
                "time": "T-0721",
                "trigger_index": 1,
                "template_index": 1,
            }
        ],
    },
    {
        "doc_id": "D03",
        "source": "city_weather",
        "events": [
            {
                "local_id": "E01",
                "event_type": "typhoon_landfall",
                "hazard": "H-ST",
                "location": "L-QG",
                "organization": "O-CITY",
                "time": "T-0722",
                "trigger_index": 0,
                "template_index": 2,
            }
        ],
    },
    {
        "doc_id": "D04",
        "source": "provincial_emergency",
        "events": [
            {
                "local_id": "E01",
                "event_type": "typhoon_landfall",
                "hazard": "H-HY",
                "location": "L-LH",
                "organization": "O-PROV",
                "time": "T-0722",
                "trigger_index": 0,
                "template_index": 0,
            }
        ],
    },
    {
        "doc_id": "D05",
        "source": "national_news",
        "events": [
            {
                "local_id": "E01",
                "event_type": "typhoon_landfall",
                "hazard": "H-HY",
                "location": "L-LH",
                "organization": "O-PROV",
                "time": "T-0721",
                "trigger_index": 0,
                "template_index": 1,
            }
        ],
    },
    {
        "doc_id": "D06",
        "source": "city_weather",
        "events": [
            {
                "local_id": "E01",
                "event_type": "flood_response",
                "location": "L-QG",
                "organization": "O-QG",
                "time": "T-0723",
                "trigger_index": 0,
                "template_index": 0,
            }
        ],
    },
    {
        "doc_id": "D07",
        "source": "national_news",
        "events": [
            {
                "local_id": "E01",
                "event_type": "flood_response",
                "location": "L-QG",
                "organization": "O-QG",
                "time": "T-0723",
                "trigger_index": 0,
                "template_index": 1,
            }
        ],
    },
]


class _StableRandom:
    """Hash-derived PRNG so output is stable across Python versions."""

    def __init__(self, seed: str, *labels: str) -> None:
        self.seed = seed
        self.labels = labels
        self.counter = 0

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            return 0
        material = "|".join(
            [self.seed, *self.labels, str(self.counter)]
        ).encode("utf-8")
        self.counter += 1
        number = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return number % upper


class DocumentGenerator:
    def __init__(self, root: str | Path, seed: str = "eventgraph-2026") -> None:
        self.root = Path(root)
        self.seed = seed
        self.vocab = read_json(self.root / "data" / "vocab.json")

    def generate_all(self, output_dir: str | Path | None = None) -> list[dict[str, Any]]:
        target = Path(output_dir or self.root / "data" / "generated" / "docs")
        target.mkdir(parents=True, exist_ok=True)
        documents = []
        for index, scenario in enumerate(SCENARIOS, start=1):
            document = self.generate_document(scenario, index)
            path = target / f"{document['doc_id']}.json"
            atomic_write_json(path, document)
            data = path.read_bytes()
            documents.append(
                {
                    "doc_id": document["doc_id"],
                    "doc_index": index,
                    "path": str(path),
                    "content_sha256": text_hash(document["content"]),
                    "file_sha256": hashlib.sha256(data).hexdigest(),
                    "source": document["source"],
                    "event_count": len(document["sentences"][0]["event_specs"]),
                }
            )
        manifest = {
            "seed": self.seed,
            "vocab_version": self.vocab["version"],
            "document_count": len(documents),
            "documents": documents,
            "manifest_sha256": hashlib.sha256(
                canonical_bytes(documents)
            ).hexdigest(),
        }
        atomic_write_json(target.parent / "manifest.json", manifest)
        return documents

    def generate_document(
        self,
        scenario: dict[str, Any],
        doc_index: int,
    ) -> dict[str, Any]:
        events = []
        pieces = []
        for event_index, event_scenario in enumerate(scenario["events"], start=1):
            rng = _StableRandom(
                self.seed,
                scenario["doc_id"],
                event_scenario["local_id"],
                str(event_index),
            )
            event_type = event_scenario["event_type"]
            trigger_options = self.vocab["triggers"][event_type]
            trigger_index = event_scenario.get("trigger_index")
            if trigger_index is None:
                trigger_index = rng.randbelow(len(trigger_options))
            trigger = dict(trigger_options[int(trigger_index)])

            template_options = self.vocab["templates"][event_type]
            template_index = event_scenario.get("template_index")
            if template_index is None:
                template_index = rng.randbelow(len(template_options))
            template = template_options[int(template_index)]

            time_entry = self.vocab["times"][event_scenario["time"]]
            location = self.vocab["entities"][event_scenario["location"]]
            organization = self.vocab["entities"][event_scenario["organization"]]
            format_values: dict[str, str] = {
                "time": time_entry["surface"],
                "location": location["name"],
                "org": organization["name"],
                "trigger": trigger["surface"],
            }
            arguments: list[dict[str, str]] = []
            if "hazard" in event_scenario:
                hazard = self.vocab["entities"][event_scenario["hazard"]]
                format_values["hazard_label"] = hazard["label"]
                arguments.append(
                    {
                        "role": "主体",
                        "kind": "entity",
                        "entity_id": event_scenario["hazard"],
                    }
                )
            arguments.extend(
                [
                    {
                        "role": "地点",
                        "kind": "entity",
                        "entity_id": event_scenario["location"],
                    },
                    {
                        "role": "机构",
                        "kind": "entity",
                        "entity_id": event_scenario["organization"],
                    },
                    {
                        "role": "时间",
                        "kind": "time",
                        "time_id": event_scenario["time"],
                    },
                ]
            )
            text = template.format(**format_values)
            pieces.append(text)
            events.append(
                {
                    "local_id": event_scenario["local_id"],
                    "event_type": event_type,
                    "template_index": int(template_index),
                    "trigger": trigger,
                    "arguments": arguments,
                }
            )
        content = "\n".join(pieces)
        return {
            "schema_version": "doc-v1",
            "doc_id": scenario["doc_id"],
            "doc_index": doc_index,
            "source": scenario["source"],
            "content": content,
            "sentences": [
                {
                    "sentence_id": f"{scenario['doc_id']}-S01",
                    "text": content,
                    "event_specs": events,
                }
            ],
        }

    def revise_document_argument(
        self,
        document: dict[str, Any],
        *,
        local_id: str,
        role: str,
        new_entity_id: str,
    ) -> dict[str, Any]:
        revised = {
            **document,
            "sentences": [
                {**sentence, "event_specs": [dict(event) for event in sentence["event_specs"]]}
                for sentence in document["sentences"]
            ],
        }
        new_entity = self.vocab["entities"][new_entity_id]
        new_value = new_entity["name"]
        sentence = revised["sentences"][0]
        matched = False
        for event in sentence["event_specs"]:
            if event["local_id"] != local_id:
                continue
            for argument in event["arguments"]:
                if argument["role"] != role:
                    continue
                old_entity_id = argument["entity_id"]
                old_value = self.vocab["entities"][old_entity_id]["name"]
                old_sentence = sentence["text"]
                if old_value not in old_sentence:
                    continue
                sentence["text"] = old_sentence.replace(old_value, new_value, 1)
                argument["entity_id"] = new_entity_id
                matched = True
                break
            if matched:
                break
        if not matched:
            raise ValueError(
                f"document {document['doc_id']} has no entity argument "
                f"{local_id}/{role}"
            )
        revised["content"] = "\n".join(part["text"] for part in revised["sentences"])
        return revised

    def revise_document_template(
        self,
        document: dict[str, Any],
        *,
        local_id: str,
        template_index: int,
    ) -> dict[str, Any]:
        revised = {
            **document,
            "sentences": [
                {**sentence, "event_specs": [dict(event) for event in sentence["event_specs"]]}
                for sentence in document["sentences"]
            ],
        }
        sentence = revised["sentences"][0]
        event_spec = next(
            event
            for event in sentence["event_specs"]
            if event["local_id"] == local_id
        )
        event_type = event_spec["event_type"]
        templates = self.vocab["templates"][event_type]
        template = templates[int(template_index)]
        format_values: dict[str, str] = {
            "trigger": event_spec["trigger"]["surface"],
        }
        for argument in event_spec["arguments"]:
            if argument["kind"] == "time":
                entry = self.vocab["times"][argument["time_id"]]
                format_values["time"] = entry["surface"]
                continue
            entry = self.vocab["entities"][argument["entity_id"]]
            if argument["role"] == "地点":
                format_values["location"] = entry["name"]
            elif argument["role"] == "机构":
                format_values["org"] = entry["name"]
            elif argument["role"] == "主体":
                format_values["hazard_label"] = entry["label"]
        sentence["text"] = template.format(**format_values)
        revised["content"] = "\n".join(part["text"] for part in revised["sentences"])
        if revised["content"] == document["content"]:
            raise ValueError("revised template produced identical document content")
        return revised
