"""生成确定性本地输入：样本、模型参数、调度配置、异常注入配置。

运行：python scripts/generate_data.py
全部使用固定随机种子，重复生成字节级一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dp_bench.model import ACTIONS

DATA = Path(__file__).resolve().parent.parent / "data"
SEED = 20261117
FEATURE_DIM = 64

# 12 条确定性中文样本（覆盖不同长度与词性组合）
SENTENCES = [
    ("S01", [("经济", "NN"), ("建设", "NN"), ("取得", "VV"), ("成就", "NN")]),
    ("S02", [("我们", "PN"), ("热爱", "VV"), ("伟大", "JJ"), ("祖国", "NN")]),
    ("S03", [("技术", "NN"), ("创新", "NN"), ("推动", "VV"), ("产业", "NN"),
             ("升级", "NN")]),
    ("S04", [("市场", "NN"), ("需求", "NN"), ("持续", "AD"), ("增长", "VV")]),
    ("S05", [("科学家", "NN"), ("提出", "VV"), ("新", "JJ"), ("理论", "NN")]),
    ("S06", [("孩子", "NN"), ("认真", "AD"), ("阅读", "VV"), ("课外", "JJ"),
             ("读物", "NN")]),
    ("S07", [("政策", "NN"), ("支持", "VV"), ("中小", "JJ"), ("企业", "NN"),
             ("发展", "VV")]),
    ("S08", [("铁路", "NN"), ("建设", "NN"), ("促进", "VV"), ("区域", "NN"),
             ("协调", "NN"), ("发展", "NN")]),
    ("S09", [("数据", "NN"), ("要素", "NN"), ("成为", "VV"), ("关键", "JJ"),
             ("生产", "NN"), ("要素", "NN")]),
    ("S10", [("绿色", "JJ"), ("能源", "NN"), ("替代", "VV"), ("传统", "JJ"),
             ("化石", "NN"), ("能源", "NN")]),
    ("S11", [("高校", "NN"), ("毕业生", "NN"), ("积极", "AD"), ("投身", "VV"),
             ("基层", "NN"), ("实践", "NN")]),
    ("S12", [("人工智能", "NN"), ("技术", "NN"), ("深刻", "AD"), ("改变", "VV"),
             ("社会", "NN"), ("治理", "NN"), ("模式", "NN")]),
]

# 异常样本：未知词性 XX，用于触发特征阶段异常隔离
EXCEPTION_SENTENCE = ("S90", [("未知", "XX"), ("词", "NN")])

POS_TAGS = ["NN", "VV", "JJ", "AD", "PN", "ROOT"]


def build_model() -> dict:
    rng = np.random.default_rng(SEED)
    words = sorted({w for _, toks in SENTENCES for w, _ in toks}
                   | {"<NULL>", "<ROOT>"})
    word_hash = {word: int(rng.integers(0, FEATURE_DIM)) for word in words}
    pos_hash = {pos: int(rng.integers(0, FEATURE_DIM // 2))
                for pos in POS_TAGS + ["<NULL>"]}
    weights = {
        action: [round(float(x), 6)
                 for x in rng.normal(0.0, 1.0, FEATURE_DIM)]
        for action in ACTIONS
    }
    return {
        "feature_dim": FEATURE_DIM,
        "hash_buckets": FEATURE_DIM,
        "word_hash": word_hash,
        "pos_hash": pos_hash,
        "weights": weights,
    }


def write_samples(path: Path, include_exception: bool = False) -> None:
    lines = []
    for sid, toks in SENTENCES:
        lines.append(sid + " " + " ".join(f"{w}/{p}" for w, p in toks))
    if include_exception:
        sid, toks = EXCEPTION_SENTENCE
        lines.append(sid + " " + " ".join(f"{w}/{p}" for w, p in toks))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_injections(path: Path) -> None:
    base_model = build_model()
    bad_dim_model = dict(base_model)
    bad_dim_model["hash_buckets"] = FEATURE_DIM - 1
    bad_weight_model = {
        **base_model,
        "weights": {**base_model["weights"],
                    "SHIFT": base_model["weights"]["SHIFT"][:-1]},
    }
    spec = {
        "injections": [
            {
                "injection_id": "INJ-01",
                "kind": "config",
                "description": "调度配置缺少 batch_size 字段",
                "expected_code": "E_CONFIG_INVALID",
                "payload": {
                    "max_workers": 4,
                    "max_steps_per_sentence": 64,
                    "global_step_budget": 10000,
                    "per_sentence_quota": 64,
                    "timeout_observe_ms": 50,
                },
            },
            {
                "injection_id": "INJ-02",
                "kind": "config",
                "description": "并发数非正（max_workers=0）",
                "expected_code": "E_CONFIG_INVALID",
                "payload": {
                    "max_workers": 0,
                    "max_steps_per_sentence": 64,
                    "global_step_budget": 10000,
                    "per_sentence_quota": 64,
                    "timeout_observe_ms": 50,
                    "batch_size": 4,
                },
            },
            {
                "injection_id": "INJ-03",
                "kind": "config",
                "description": "全局预算越界（global_step_budget<=0）",
                "expected_code": "E_CONFIG_INVALID",
                "payload": {
                    "max_workers": 4,
                    "max_steps_per_sentence": 64,
                    "global_step_budget": 0,
                    "per_sentence_quota": 64,
                    "timeout_observe_ms": 50,
                    "batch_size": 4,
                },
            },
            {
                "injection_id": "INJ-04",
                "kind": "model",
                "description": "特征维度与哈希桶数不符",
                "expected_code": "E_MODEL_SHAPE_MISMATCH",
                "payload": bad_dim_model,
            },
            {
                "injection_id": "INJ-05",
                "kind": "model",
                "description": "动作权重向量形状与特征维度不符",
                "expected_code": "E_MODEL_SHAPE_MISMATCH",
                "payload": bad_weight_model,
            },
            {
                "injection_id": "INJ-06",
                "kind": "sample",
                "description": "样本行词与词性数量不匹配（缺少词性标记）",
                "expected_code": "E_SAMPLE_MALFORMED",
                "payload": {"lines": ["S99 经济 建设/NN 取得/VV"]},
            },
            {
                "injection_id": "INJ-07",
                "kind": "sample",
                "description": "样本行只有句 ID 没有 token",
                "expected_code": "E_SAMPLE_MALFORMED",
                "payload": {"lines": ["S99"]},
            },
        ]
    }
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    model = build_model()
    (DATA / "model.json").write_text(
        json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    write_samples(DATA / "sentences.txt")
    write_samples(DATA / "sentences_exception.txt", include_exception=True)

    configs = {
        "config.json": {
            "max_workers": 4,
            "max_steps_per_sentence": 64,
            "global_step_budget": 10000,
            "per_sentence_quota": 64,
            "timeout_observe_ms": 50,
            "batch_size": 4,
        },
        "config_budget.json": {
            "max_workers": 4,
            "max_steps_per_sentence": 64,
            "global_step_budget": 40,
            "per_sentence_quota": 64,
            "timeout_observe_ms": 50,
            "batch_size": 4,
        },
        "config_steplimit.json": {
            "max_workers": 4,
            "max_steps_per_sentence": 6,
            "global_step_budget": 10000,
            "per_sentence_quota": 64,
            "timeout_observe_ms": 50,
            "batch_size": 4,
        },
    }
    for name, cfg in configs.items():
        (DATA / name).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    write_injections(DATA / "injections.json")
    print(f"已在 {DATA} 生成确定性输入文件")


if __name__ == "__main__":
    main()
