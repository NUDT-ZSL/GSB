"""文档/查询/翻译表加载、字段校验与归一化阶段。"""

from .errors import E_RECORD_INVALID, RecordInvalid, make_failure
from .io_utils import read_jsonl
from .text import build_vocab, detect_language, normalize_text, Tokenizer

STAGE = "normalize"


def load_translations(path):
    pairs = read_jsonl(path)
    seen = set()
    for index, pair in enumerate(pairs):
        for field in ("source", "target", "confidence"):
            if field not in pair or pair[field] is None or str(pair[field]).strip() == "":
                raise RecordInvalid(
                    f"translation row {index} missing {field}", E_RECORD_INVALID
                )
        pair["confidence"] = float(pair["confidence"])
        key = (pair["source"], pair["target"])
        if key in seen:
            raise RecordInvalid(f"duplicate translation pair {key}", E_RECORD_INVALID)
        seen.add(key)
    return pairs


def _validate_record(record, kind, id_field, text_fields, allowed_langs, seen_ids):
    record_id = record.get(id_field, f"<missing-{id_field}>")
    if not isinstance(record, dict):
        raise RecordInvalid("record is not an object")
    if not record_id or not str(record_id).strip():
        raise RecordInvalid(f"{kind} id is empty")
    if record_id in seen_ids:
        raise RecordInvalid(f"duplicate {kind} id: {record_id}")
    lang = record.get("lang")
    if lang not in allowed_langs:
        raise RecordInvalid(f"lang '{lang}' not in {allowed_langs}")
    for field in text_fields:
        value = record.get(field)
        if value is None or not str(value).strip():
            raise RecordInvalid(f"field '{field}' is empty")
    seen_ids.add(record_id)
    return record_id


def normalize_corpus(kind, raw_rows, config, tokenizer, translation_vocab, seen_ids=None):
    """校验并归一化一批文档或查询，返回（归一化条目, 失败条目）。

    ``seen_ids`` 由调用方跨批次持有，保证跨批次重复 ID 同样被拒绝。
    """
    allowed = config["languages"]["allowed"]
    rules = config["normalization"]
    id_field = "doc_id" if kind == "document" else "query_id"
    text_fields = ["title", "body"] if kind == "document" else ["text"]
    if seen_ids is None:
        seen_ids = set()
    normalized = []
    failures = []

    for index, record in enumerate(raw_rows):
        item_id = record.get(id_field) if isinstance(record, dict) else None
        try:
            record_id = _validate_record(
                record, kind, id_field, text_fields, allowed, seen_ids
            )
            if kind == "document":
                title = normalize_text(record["title"], rules)
                body = normalize_text(record["body"], rules)
                combined = f"{title} {body}"
            else:
                title = ""
                body = ""
                combined = normalize_text(record["text"], rules)
            tokens = tokenizer.tokenize(combined)
            detected_lang, evidence = (
                detect_language(combined, tokens, config, translation_vocab)
                if config["language_identification"]["enabled"]
                else (record["lang"], {"rule": "identification_disabled"})
            )
            entry = {
                id_field: record_id,
                "declared_lang": record["lang"],
                "detected_lang": detected_lang,
                "lang_evidence": evidence,
                "tokens": tokens,
            }
            if kind == "document":
                entry["title"] = title
                entry["body"] = body
            else:
                entry["text"] = combined
            normalized.append(entry)
        except RecordInvalid as exc:
            failures.append(
                make_failure(
                    item_id if item_id is not None else f"<row-{index}>",
                    STAGE,
                    E_RECORD_INVALID,
                    type(exc).__name__,
                    str(exc),
                )
            )
        except Exception as exc:  # 异常隔离：单条失败不拖垮整批
            failures.append(
                make_failure(
                    item_id if item_id is not None else f"<row-{index}>",
                    STAGE,
                    E_RECORD_INVALID,
                    type(exc).__name__,
                    str(exc),
                )
            )
    normalized.sort(key=lambda item: item[id_field])
    return normalized, failures


def build_tokenizer(translation_pairs, config):
    vocab = build_vocab(translation_pairs, config)
    stopwords = set()
    for lang in config["languages"]["allowed"]:
        stopwords.update(config["tokenization"].get("stopwords", {}).get(lang, []))
    return Tokenizer(vocab, stopwords)


def load_all(config):
    """一次性读取原始输入（归一化本身仍按批次执行与快照）。"""
    paths = config["paths"]
    translations = load_translations(paths["translations"])
    raw_docs = read_jsonl(paths["documents"])
    raw_queries = read_jsonl(paths["queries"])
    tokenizer = build_tokenizer(translations, config)
    translation_vocab = set()
    for pair in translations:
        translation_vocab.add(pair["source"])
        translation_vocab.add(pair["target"])
    return {
        "translations": translations,
        "raw_docs": raw_docs,
        "raw_queries": raw_queries,
        "tokenizer": tokenizer,
        "translation_vocab": translation_vocab,
    }
