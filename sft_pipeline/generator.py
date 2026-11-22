from __future__ import annotations

from typing import Any

from .util import atomic_write_bytes, atomic_write_text, stable_hex, stable_int
from .validation import load_bundle
from .workspace import Workspace


GENERATION_SEED = 20261123

SOURCE_SPECS = (
    {
        "source_tag": "general_en",
        "lang": "en",
        "topics": ("water cycles", "city recycling", "study routines", "sleep hygiene"),
    },
    {
        "source_tag": "code_en",
        "lang": "en",
        "topics": ("CSV parsing", "binary search", "HTTP retries", "SQL indexes"),
    },
    {
        "source_tag": "dialogue_zh",
        "lang": "zh",
        "topics": ("退款流程", "行程变更", "账号安全", "产品安装"),
    },
    {
        "source_tag": "knowledge_ja",
        "lang": "ja",
        "topics": ("火山形成", "图书馆分类", "海洋潮汐", "睡眠周期"),
    },
)

DETAIL_TOKENS = (
    "amber-ledger",
    "copper-orbit",
    "quiet-harbor",
    "maple-signal",
    "silver-compass",
    "cobalt-lattice",
    "willow-index",
    "granite-pulse",
)

EN_INSTRUCTIONS = (
    "Explain the concept clearly for a beginner.",
    "Give a concise practical answer with one example.",
    "Summarize the key points and note one caveat.",
    "Describe a reliable step-by-step approach.",
)
ZH_INSTRUCTIONS = (
    "请用简洁步骤说明处理方法。",
    "给出面向新手的解释，并补充一个注意事项。",
    "请先总结要点，再给出可执行建议。",
    "请分析问题并提供一个示例。",
)
JA_INSTRUCTIONS = (
    "初心者にも分かるように説明してください。",
    "要点を短くまとめ、注意点を一つ添えてください。",
    "実行可能な手順として整理してください。",
    "例を一つ挙げて分かりやすく説明してください。",
)


def _build_templates() -> dict[str, Any]:
    return {
        "library_version": "tpl-2026.11.1",
        "selection": {"mode": "stable_hash", "salt": "route-v1"},
        "templates": [
            {
                "template_id": "T-GEN-A",
                "source_tags": ["general_en"],
                "pattern": (
                    "### Task\n{instruction}\n\n### Context\n{input}\n\n"
                    "### Answer\n{output}"
                ),
                "enabled": True,
            },
            {
                "template_id": "T-GEN-B",
                "source_tags": ["general_en"],
                "pattern": (
                    "Instruction: {instruction}\nInput: {input}\n"
                    "Response: {output}\nEnd."
                ),
                "enabled": True,
            },
            {
                "template_id": "T-CODE-A",
                "source_tags": ["code_en"],
                "pattern": (
                    "<task>{instruction}</task>\n<context>{input}</context>\n"
                    "<solution>{output}</solution>"
                ),
                "enabled": True,
            },
            {
                "template_id": "T-CODE-B",
                "source_tags": ["code_en"],
                "pattern": (
                    "QUESTION\n{instruction}\nDETAILS\n{input}\nANSWER\n{output}"
                ),
                "enabled": True,
            },
            {
                "template_id": "T-ZH-A",
                "source_tags": ["dialogue_zh"],
                "pattern": "【任务】{instruction}\n【背景】{input}\n【回答】{output}",
                "enabled": True,
            },
            {
                "template_id": "T-ZH-B",
                "source_tags": ["dialogue_zh"],
                "pattern": "用户：{instruction}\n场景：{input}\n助手：{output}",
                "enabled": True,
            },
            {
                "template_id": "T-JA-A",
                "source_tags": ["knowledge_ja"],
                "pattern": "依頼: {instruction}\n前提: {input}\n回答: {output}",
                "enabled": True,
            },
        ],
    }


def _build_rules() -> dict[str, Any]:
    return {
        "version": "clean-2026.11.1",
        "quality": {
            "version": "qw-2026.11.1",
            "length_target": 420,
            "ngram_size": 4,
            "weights": {"length": 0.45, "repetition": 0.30, "noise": 0.25},
            "noise_chars": "~^`|#@",
            "tiers": [
                {"name": "high", "min_score": 0.78},
                {"name": "medium", "min_score": 0.58},
                {"name": "low", "min_score": 0.0},
            ],
        },
        "dedupe": {
            "threshold": 0.88,
            "shingle_size": 5,
            "signature_size": 64,
        },
    }


def _build_quotas() -> dict[str, Any]:
    return {
        "version": "quota-2026.11.1",
        "seed": 731993,
        "total_target": 30,
        "targets": [
            {"source_tag": "general_en", "quality_tier": "high", "target": 5},
            {"source_tag": "general_en", "quality_tier": "medium", "target": 3},
            {"source_tag": "general_en", "quality_tier": "low", "target": 1},
            {"source_tag": "code_en", "quality_tier": "high", "target": 5},
            {"source_tag": "code_en", "quality_tier": "medium", "target": 3},
            {"source_tag": "code_en", "quality_tier": "low", "target": 1},
            {"source_tag": "dialogue_zh", "quality_tier": "high", "target": 3},
            {"source_tag": "dialogue_zh", "quality_tier": "medium", "target": 2},
            {"source_tag": "dialogue_zh", "quality_tier": "low", "target": 1},
            {"source_tag": "knowledge_ja", "quality_tier": "high", "target": 3},
            {"source_tag": "knowledge_ja", "quality_tier": "medium", "target": 2},
            {"source_tag": "knowledge_ja", "quality_tier": "low", "target": 1},
        ],
    }


def _build_pipeline_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "allowed_langs": ["en", "ja", "zh"],
        "required_fields": [
            "sample_id",
            "instruction",
            "input",
            "output",
            "source_tag",
            "quality_flag",
            "lang",
            "annotation_revision",
        ],
        "annotation_revision_pattern": "^[0-9]+$",
        "export": {
            "dataset_name": "local-sft",
            "format": "jsonl",
            "format_version": "1.0",
        },
        "limits": {
            "max_samples_per_stage": 5000,
            "max_resident_memory_mb": 256,
            "max_parallel_stages": 2,
            "requested_parallel_stages": 2,
        },
    }


def _outputs_for(source: dict[str, Any], index: int) -> tuple[str, str, str]:
    lang = source["lang"]
    topic = source["topics"][index % len(source["topics"])]
    if lang == "en":
        instruction = EN_INSTRUCTIONS[index % len(EN_INSTRUCTIONS)]
        context = f"Scenario {index + 1}: a user asks about {topic}."
        body = (
            f"Start with the goal: make {topic} understandable without hidden "
            "assumptions. Then separate the idea into inputs, process, and result. "
            "Use a concrete example, mention a common failure mode, and finish with "
            "one verification step the reader can perform locally."
        )
    elif lang == "zh":
        instruction = ZH_INSTRUCTIONS[index % len(ZH_INSTRUCTIONS)]
        context = f"场景 {index + 1}：用户正在咨询{topic}。"
        body = (
            f"先说明目标，让对方理解{topic}的关键限制；再按准备、执行、校验"
            "三个步骤组织回答。给出一个贴近日常的示例，指出最常见错误，"
            "最后提供一个无需联网即可完成的核对方法。"
        )
    else:
        instruction = JA_INSTRUCTIONS[index % len(JA_INSTRUCTIONS)]
        context = f"状況 {index + 1}: 利用者が{topic}について質問しています。"
        body = (
            f"{topic}の目的を最初に示し、前提条件、手順、確認方法の順で説明します。"
            "身近な例を一つ加え、よくある失敗と、ローカル環境で実行できる確認手順を"
            "最後に示してください。"
        )

    token_left = DETAIL_TOKENS[index % len(DETAIL_TOKENS)]
    token_right = DETAIL_TOKENS[(index * 3 + 1) % len(DETAIL_TOKENS)]
    token_third = DETAIL_TOKENS[(index * 5 + 3) % len(DETAIL_TOKENS)]
    token_fourth = DETAIL_TOKENS[(index * 7 + 5) % len(DETAIL_TOKENS)]
    local_metric = 1000 + index * 37
    local_counter = 41 + (index * 19) % 97
    unique_clause = (
        f" Case R{index:04d} records checkpoints {token_left}, {token_right}, "
        f"{token_third}, and {token_fourth}; local metric {local_metric} "
        f"with counter {local_counter}; reviewer note {token_third}."
        if lang == "en"
        else (
            f" 记录 R{index:04d} 的核对标记为 {token_left}、{token_right}、"
            f"{token_third} 与 {token_fourth}；本地指标 {local_metric}，"
            f"计数 {local_counter}；复核说明 {token_fourth}。"
            if lang == "zh"
            else (
                f" 記録 R{index:04d} の確認記号は {token_left}、{token_right}、"
                f"{token_third}、{token_fourth} です。ローカル指標は "
                f"{local_metric}、カウンタは {local_counter}、"
                f"レビュー注記は {token_third} です。"
            )
        )
    )
    variant = stable_int(GENERATION_SEED, index, "output-variant") % 4
    if variant == 0:
        output = f"{body}{unique_clause}"
    elif variant == 1:
        output = f"{body}{unique_clause} Keep the answer factual and reproducible."
    elif variant == 2:
        output = f"Brief preface. {body}{unique_clause}"
    else:
        output = (
            f"{body}{unique_clause} "
            "Note: verify each intermediate result before continuing."
        )

    if index % 15 == 0:
        output = f"{token_left}."
    elif index % 5 == 2:
        if lang == "en":
            output = (
                f"Quick note R{index:04d}: first inspect {token_left}, then "
                f"compare {token_right}, and finally record {token_third}. "
                f"Keep metric {local_metric} as the local checkpoint."
            )
        elif lang == "zh":
            output = (
                f"简要记录 R{index:04d}：先检查 {token_left}，再对比 "
                f"{token_right}，最后登记 {token_third}。本地指标为 "
                f"{local_metric}，完成后复核一次。"
            )
        else:
            output = (
                f"簡易メモ R{index:04d}: 最初に {token_left} を確認し、"
                f"次に {token_right} と比較し、最後に {token_third} を記録します。"
                f"ローカル指標は {local_metric} です。"
            )
    return instruction, context, output


def _build_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []

    total = 60
    for offset in range(total):
        index = offset
        source = SOURCE_SPECS[index % len(SOURCE_SPECS)]
        instruction, context, output = _outputs_for(source, index)

        if index % 9 == 4 and index >= 4:
            same_source = samples[index - 4]
            instruction = same_source["instruction"]
            context = same_source["input"]
            output = same_source["output"]
        elif index % 17 == 7 and index >= 4:
            same_source = samples[index - 4]
            output = f"{same_source['output']} Verify the final value twice."
        elif index % 7 == 0:
            output = f"{output} {output}"
        elif index % 11 == 0:
            output = f"{output} ~~~ @@ ##"

        revision: int | str = (index % 4) + 1
        if index % 5 == 0:
            revision = str(revision)
        sample = {
            "sample_id": f"S{index + 1:04d}",
            "instruction": instruction,
            "input": context,
            "output": output,
            "source_tag": source["source_tag"],
            "quality_flag": "review" if index % 6 == 0 else "good",
            "lang": source["lang"],
            "annotation_revision": revision,
        }
        samples.append(sample)

    return sorted(
        samples,
        key=lambda record: stable_hex(
            GENERATION_SEED, "file-order", record["sample_id"]
        ),
    )


def _json_pretty_bytes(value: Any) -> bytes:
    import json

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def generate_dataset_files(workspace: Workspace, *, force: bool = False) -> dict[str, Any]:
    workspace.ensure_layout()
    if workspace.inputs_exist() and not force:
        return {
            "ok": True,
            "generated": False,
            "reason": "inputs already exist",
            "seed": GENERATION_SEED,
        }

    templates = _build_templates()
    rules = _build_rules()
    quotas = _build_quotas()
    pipeline_config = _build_pipeline_config()
    samples = _build_samples()

    from .util import canonical_json_text

    sample_bytes = "".join(
        canonical_json_text(record) + "\n" for record in samples
    ).encode("utf-8")
    atomic_write_bytes(workspace.samples_path, sample_bytes)
    atomic_write_bytes(workspace.templates_path, _json_pretty_bytes(templates))
    atomic_write_bytes(workspace.rules_path, _json_pretty_bytes(rules))
    atomic_write_bytes(workspace.quotas_path, _json_pretty_bytes(quotas))
    atomic_write_bytes(
        workspace.pipeline_config_path, _json_pretty_bytes(pipeline_config)
    )
    atomic_write_text(
        workspace.data_dir / "seed.json",
        '{"generation_seed":20261123,"sample_count":60,"schema_version":1}\n',
    )

    bundle = load_bundle(workspace)
    return {
        "ok": True,
        "generated": True,
        "seed": GENERATION_SEED,
        "sample_count": len(bundle.samples),
        "template_count": len(templates["templates"]),
        "quota_total": quotas["total_target"],
        "samples_digest": bundle.samples_digest,
    }
