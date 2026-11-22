from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import fail
from .utils import content_hash, read_json, text_hash


class DocumentParser:
    def __init__(self, root: str | Path, parser_version: str) -> None:
        self.root = Path(root)
        self.vocab = read_json(self.root / "data" / "vocab.json")
        self.parser_version = parser_version

    def parse(self, document: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(document["doc_id"])
        doc_index = int(document["doc_index"])
        content = str(document["content"])
        if text_hash("\n".join(part["text"] for part in document["sentences"])) != text_hash(content):
            fail(
                "E_SPAN_INVALID",
                "文档内容与句子文本不一致",
                document_id=doc_id,
                event_id="-",
                field="content",
            )

        mentions: list[dict[str, Any]] = []
        cursor = 0
        for sentence in document["sentences"]:
            sentence_text = str(sentence["text"])
            sentence_start = content.find(sentence_text, cursor)
            if sentence_start < 0:
                fail(
                    "E_SPAN_INVALID",
                    "句子文本在文档中不存在",
                    document_id=doc_id,
                    event_id="-",
                    field="sentence",
                    sentence_id=sentence["sentence_id"],
                )
            cursor = sentence_start + len(sentence_text)
            for entity_id, entity in self.vocab["entities"].items():
                start = sentence_start
                while True:
                    relative = content.find(entity["name"], start, cursor)
                    if relative < 0:
                        break
                    mentions.append(
                        self._mention(
                            doc_id,
                            entity_id,
                            entity["name"],
                            entity["kind"],
                            relative,
                            relative + len(entity["name"]),
                            sentence["sentence_id"],
                        )
                    )
                    start = relative + len(entity["name"])
            for time_id, time_entry in self.vocab["times"].items():
                start = sentence_start
                while True:
                    relative = content.find(time_entry["surface"], start, cursor)
                    if relative < 0:
                        break
                    mentions.append(
                        self._mention(
                            doc_id,
                            time_id,
                            time_entry["surface"],
                            "time",
                            relative,
                            relative + len(time_entry["surface"]),
                            sentence["sentence_id"],
                            normalized=time_entry["normalized"],
                        )
                    )
                    start = relative + len(time_entry["surface"])
        mentions.sort(
            key=lambda item: (
                item["start"],
                item["end"],
                item["mention_id"],
            )
        )

        events: list[dict[str, Any]] = []
        seen_local_ids: set[str] = set()
        occupied_triggers: dict[tuple[int, int], str] = {}
        for sentence in document["sentences"]:
            sentence_text = str(sentence["text"])
            sentence_start = content.find(sentence_text)
            for spec in sentence["event_specs"]:
                event_id = f"{doc_id}:{spec['local_id']}"
                if spec["local_id"] in seen_local_ids:
                    fail(
                        "E_SPAN_INVALID",
                        "文档内局部事件编号重复",
                        document_id=doc_id,
                        event_id=event_id,
                        reason="duplicate_local_event_id",
                    )
                seen_local_ids.add(spec["local_id"])
                trigger_surface = spec["trigger"]["surface"]
                override = spec.get("trigger_span_override")
                if override is not None:
                    trigger_start = int(override["start"])
                    trigger_end = int(override["end"])
                    self._validate_span(
                        doc_id,
                        event_id,
                        "trigger",
                        trigger_start,
                        trigger_end,
                        len(content),
                    )
                    if content[trigger_start:trigger_end] != trigger_surface:
                        fail(
                            "E_SPAN_INVALID",
                            "触发词偏移与触发词文本不匹配",
                            document_id=doc_id,
                            event_id=event_id,
                            field="trigger",
                            start=trigger_start,
                            end=trigger_end,
                            reason="trigger_span_mismatch",
                        )
                else:
                    relative = sentence_text.find(trigger_surface)
                    if relative < 0:
                        fail(
                            "E_SPAN_INVALID",
                            "触发词未出现在所属句子中",
                            document_id=doc_id,
                            event_id=event_id,
                            field="trigger",
                            reason="trigger_missing",
                        )
                    trigger_start = sentence_start + relative
                    trigger_end = trigger_start + len(trigger_surface)
                self._validate_span(
                    doc_id,
                    event_id,
                    "trigger",
                    trigger_start,
                    trigger_end,
                    len(content),
                )
                trigger_span = (trigger_start, trigger_end)
                if trigger_span in occupied_triggers:
                    fail(
                        "E_SPAN_INVALID",
                        "触发词偏移已被其他事件占用",
                        document_id=doc_id,
                        event_id=event_id,
                        field="trigger",
                        start=trigger_start,
                        end=trigger_end,
                        reason="duplicate_trigger_occupancy",
                        occupied_by=occupied_triggers[trigger_span],
                    )
                occupied_triggers[trigger_span] = event_id

                arguments = []
                for argument_spec in spec["arguments"]:
                    argument = self._resolve_argument(
                        doc_id,
                        event_id,
                        sentence_start,
                        content,
                        argument_spec,
                    )
                    arguments.append(argument)
                arguments.sort(key=lambda item: (item["start"], item["role"]))
                events.append(
                    {
                        "event_uid": event_id,
                        "document_id": doc_id,
                        "doc_index": doc_index,
                        "local_id": spec["local_id"],
                        "event_type": spec["event_type"],
                        "sentence_id": sentence["sentence_id"],
                        "trigger": {
                            "surface": trigger_surface,
                            "lemma": spec["trigger"]["lemma"],
                            "start": trigger_start,
                            "end": trigger_end,
                            "offset_unit": "unicode_code_point",
                        },
                        "arguments": arguments,
                        "order_start": trigger_start,
                    }
                )
        events.sort(
            key=lambda item: (
                item["order_start"],
                item["local_id"],
            )
        )
        for order, event in enumerate(events):
            event["order"] = order

        return {
            "parser_version": self.parser_version,
            "vocab_version": self.vocab["version"],
            "document_id": doc_id,
            "doc_index": doc_index,
            "document_digest": content_hash(
                {
                    "doc_id": doc_id,
                    "doc_index": doc_index,
                    "content": content,
                    "source": document["source"],
                }
            ),
            "content": content,
            "source": document["source"],
            "mentions": mentions,
            "events": events,
        }

    def _resolve_argument(
        self,
        doc_id: str,
        event_id: str,
        sentence_start: int,
        content: str,
        argument_spec: dict[str, Any],
    ) -> dict[str, Any]:
        kind = argument_spec["kind"]
        if kind == "entity":
            vocab_entry = self.vocab["entities"][argument_spec["entity_id"]]
            value = vocab_entry["name"]
            source_id = argument_spec["entity_id"]
            normalized = None
        else:
            vocab_entry = self.vocab["times"][argument_spec["time_id"]]
            value = vocab_entry["surface"]
            source_id = argument_spec["time_id"]
            normalized = vocab_entry["normalized"]
        relative = content.find(value, sentence_start)
        if relative < 0:
            fail(
                "E_SPAN_INVALID",
                "论元值未出现在文档中",
                document_id=doc_id,
                event_id=event_id,
                field=f"argument.{argument_spec['role']}",
                value=value,
            )
        start = relative
        end = start + len(value)
        self._validate_span(
            doc_id,
            event_id,
            f"argument.{argument_spec['role']}",
            start,
            end,
            len(content),
        )
        return {
            "role": argument_spec["role"],
            "kind": kind,
            "value": value,
            "entity_id": source_id if kind == "entity" else None,
            "time_id": source_id if kind == "time" else None,
            "normalized": normalized,
            "start": start,
            "end": end,
            "offset_unit": "unicode_code_point",
        }

    @staticmethod
    def _mention(
        doc_id: str,
        mention_id: str,
        value: str,
        kind: str,
        start: int,
        end: int,
        sentence_id: str,
        *,
        normalized: str | None = None,
    ) -> dict[str, Any]:
        return {
            "document_id": doc_id,
            "mention_id": mention_id,
            "value": value,
            "kind": kind,
            "normalized": normalized,
            "start": start,
            "end": end,
            "offset_unit": "unicode_code_point",
            "sentence_id": sentence_id,
        }

    @staticmethod
    def _validate_span(
        doc_id: str,
        event_id: str,
        field: str,
        start: int,
        end: int,
        content_length: int,
    ) -> None:
        if start < 0 or end <= start or end > content_length:
            fail(
                "E_SPAN_INVALID",
                "码点偏移越界或区间非法",
                document_id=doc_id,
                event_id=event_id,
                field=field,
                start=start,
                end=end,
                content_length=content_length,
            )
