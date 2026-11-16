"""阶段化流水线：分词 -> HMM 未登录词 -> 词性标注，支持持久化恢复。"""
from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

from . import STAGES, STAGE_HMM, STAGE_POS, STAGE_SEGMENT, STAGE_VERSIONS
from .config import PipelineConfig
from .data import Lexicon, LogProbs
from .hmm import HmmIdentifier
from .numeric import DeterministicJson, round_score
from .pos_tagger import PosTagger
from .segmenter import MaximumMatchingSegmenter

VERSION_MISMATCH_CODE = "E_VERSION_MISMATCH"


def read_input(path: str | Path) -> list[tuple[str, str]]:
    """每行“句子ID<TAB或空白>原始文本”，保持输入顺序。"""
    sentences: list[tuple[str, str]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            if "\t" in line:
                sentence_id, text = line.split("\t", 1)
            else:
                pieces = line.split(maxsplit=1)
                if len(pieces) != 2:
                    raise ValueError(
                        f"输入第 {line_no} 行缺少句子 ID 或文本: {raw!r}")
                sentence_id, text = pieces
            sentences.append((sentence_id.strip(), text))
    return sentences


class Pipeline:
    def __init__(self, config: PipelineConfig, output_dir: str | Path):
        self.config = config
        self.output_dir = Path(output_dir)
        self.lexicon = Lexicon(config.lexicon_path)
        self.hmm_model = LogProbs(config.hmm_path)
        self.pos_model = LogProbs(config.pos_path)
        self.segmenter = MaximumMatchingSegmenter(self.lexicon, config)
        self.hmm = HmmIdentifier(self.hmm_model, config)
        self.tagger = PosTagger(self.pos_model, config)
        self.config_hash = config.config_hash()

    def stage_path(self, stage: str) -> Path:
        return self.output_dir / f"stage_{stage}.json"

    def _write_stage(self, stage: str, sentences: list[dict],
                     failures: list[dict], metadata: dict) -> None:
        payload = {
            "stage": stage,
            "stage_version": STAGE_VERSIONS[stage],
            "config_hash": self.config_hash,
            "metadata": metadata,
            "sentences": sentences,
            "failures": failures,
        }
        DeterministicJson.atomic_write(self.stage_path(stage), payload)

    def _load_stage(self, stage: str) -> dict:
        with open(self.stage_path(stage), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _check_resume(self) -> tuple[str | None, dict[str, dict]]:
        """返回首个待执行阶段（None 表示全部完成）与已存在阶段数据。"""
        existing: dict[str, dict] = {}
        start_stage: str | None = STAGE_SEGMENT
        for stage in STAGES:
            path = self.stage_path(stage)
            if not path.exists():
                break
            data = self._load_stage(stage)
            if (data.get("stage_version") != STAGE_VERSIONS[stage]
                    or data.get("config_hash") != self.config_hash):
                print(
                    f"{VERSION_MISMATCH_CODE}: 阶段文件 {path.name} "
                    "版本号或配置哈希与当前不一致，拒绝继续",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            existing[stage] = data
            start_stage = (
                STAGES[STAGES.index(stage) + 1]
                if STAGES.index(stage) + 1 < len(STAGES) else None
            )
        return start_stage, existing

    def run(self, sentences: list[tuple[str, str]]) -> int:
        start_stage, existing = self._check_resume()
        all_failures: list[dict] = []

        if start_stage == STAGE_SEGMENT:
            seg_sentences, seg_failures = self._run_segmentation(sentences)
            all_failures.extend(seg_failures)
            self._write_stage(
                STAGE_SEGMENT, seg_sentences, seg_failures,
                {"max_word_length": self.config.max_word_length,
                 "unknown_confidence": self.config.unknown_confidence})
        else:
            seg_sentences = existing[STAGE_SEGMENT]["sentences"]
            all_failures.extend(existing[STAGE_SEGMENT]["failures"])

        if start_stage in (STAGE_SEGMENT, STAGE_HMM, STAGE_POS):
            hmm_sentences, hmm_failures = self._run_hmm(seg_sentences)
            all_failures.extend(hmm_failures)
            self._write_stage(
                STAGE_HMM, hmm_sentences, hmm_failures,
                {"hmm_emission_smoothing":
                 self.config.hmm_emission_smoothing})
        else:
            hmm_sentences = existing[STAGE_HMM]["sentences"]
            all_failures.extend(existing[STAGE_HMM]["failures"])

        if start_stage in (STAGE_SEGMENT, STAGE_HMM, STAGE_POS):
            pos_sentences, pos_failures = self._run_pos(hmm_sentences)
            all_failures.extend(pos_failures)
            self._write_stage(
                STAGE_POS, pos_sentences, pos_failures,
                {"pos_emission_smoothing":
                 self.config.pos_emission_smoothing,
                 "pos_transition_smoothing":
                 self.config.pos_transition_smoothing,
                 "low_confidence_threshold":
                 self.config.low_confidence_threshold})
        else:
            pos_sentences = existing[STAGE_POS]["sentences"]
            all_failures.extend(existing[STAGE_POS]["failures"])

        all_failures = sorted(
            all_failures, key=lambda item: (item["sentence_id"],
                                            item["stage"]))
        report = self._build_report(pos_sentences, all_failures)
        DeterministicJson.atomic_write(
            self.output_dir / "report.json", report)
        return 1 if all_failures else 0

    def _parallel(self, items, stage, worker):
        """线程池并行处理；单句异常隔离并转为失败清单条目。"""
        results: list = []
        failures: list[dict] = []
        ordered = sorted(items, key=lambda item: item[0])
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, self.config.max_workers)) as executor:
            futures = {
                executor.submit(worker, key, payload): key
                for key, payload in ordered
            }
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append({
                        "sentence_id": key,
                        "stage": stage,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                    })
        results.sort(key=lambda item: item["sentence_id"])
        return results, failures

    def _run_segmentation(self, sentences):
        def worker(sentence_id, text):
            return self.segmenter.segment(sentence_id, text)
        return self._parallel(sentences, STAGE_SEGMENT, worker)

    def _run_hmm(self, seg_sentences):
        def worker(sentence_id, payload):
            return self._hmm_sentence(payload)
        items = [(item["sentence_id"], item) for item in seg_sentences]
        return self._parallel(items, STAGE_HMM, worker)

    def _hmm_sentence(self, seg_result: dict) -> dict:
        tokens = seg_result["tokens"]
        final_tokens: list[dict] = []
        runs: list[dict] = []
        index = 0
        while index < len(tokens):
            if tokens[index]["in_lexicon"]:
                final_tokens.append(dict(tokens[index]))
                index += 1
                continue
            run_tokens = []
            while index < len(tokens) and not tokens[index]["in_lexicon"]:
                run_tokens.append(tokens[index])
                index += 1
            chars = [token["word"] for token in run_tokens]
            positions = [token["start"] for token in run_tokens]
            decoded = self.hmm.decode(chars, positions)
            runs.append(decoded)
            loss_by_start = {
                token["start"]: token["segmentation_loss"]
                for token in run_tokens
            }
            for word in decoded["tokens"]:
                word_loss = sum(
                    loss_by_start[pos]
                    for pos in range(word["start"], word["end"])
                )
                final_tokens.append({
                    "word": word["word"],
                    "start": word["start"],
                    "end": word["end"],
                    "in_lexicon": False,
                    "from_hmm": True,
                    "hmm_tags": word["hmm_tags"],
                    "boundary_confidence":
                        round_score(self.config.unknown_confidence),
                    "segmentation_loss": round_score(word_loss),
                })
        return {
            "sentence_id": seg_result["sentence_id"],
            "text": seg_result["text"],
            "tokens": final_tokens,
            "segmentation_log_loss": seg_result["segmentation_log_loss"],
            "hmm_runs": runs,
        }

    def _run_pos(self, hmm_sentences):
        def worker(sentence_id, payload):
            tag_result = self.tagger.tag(payload["tokens"])
            return {
                "sentence_id": sentence_id,
                "text": payload["text"],
                "tagged_tokens": tag_result["tagged_tokens"],
                "segmentation_log_loss": payload["segmentation_log_loss"],
                "hmm_runs": payload["hmm_runs"],
                "pos_total_path_score": tag_result["total_path_score"],
                "pos_log_normalized_probability":
                    tag_result["log_normalized_probability"],
                "pos_log_loss": tag_result["pos_log_loss"],
                "total_log_loss": round_score(
                    payload["segmentation_log_loss"]
                    + tag_result["pos_log_loss"]),
                "low_confidence_count": tag_result["low_confidence_count"],
                "smoothed_words": tag_result["smoothed_words"],
            }
        items = [(item["sentence_id"], item) for item in hmm_sentences]
        return self._parallel(items, STAGE_POS, worker)

    def _build_report(self, pos_sentences, failures):
        def average(values):
            values = [value for value in values if value is not None]
            if not values:
                return None
            return round_score(sum(values) / len(values))

        seg_scores = [
            item["segmentation_log_loss"] for item in pos_sentences
        ]
        hmm_scores = [
            run["viterbi_score"]
            for item in pos_sentences for run in item["hmm_runs"]
        ]
        pos_scores = [
            item["pos_total_path_score"] for item in pos_sentences
        ]
        succeeded_ids = {item["sentence_id"] for item in pos_sentences}
        failed_ids = {item["sentence_id"] for item in failures}
        return {
            "total_sentences": len(succeeded_ids) + len(failed_ids),
            "succeeded_sentences": len(succeeded_ids),
            "failed_sentences": len(failed_ids),
            "stage_average_log_scores": {
                STAGE_SEGMENT: average(seg_scores),
                STAGE_HMM: average(hmm_scores),
                STAGE_POS: average(pos_scores),
            },
            "low_confidence_word_count": sum(
                item["low_confidence_count"] for item in pos_sentences
            ),
            "config_hash": self.config_hash,
            "stage_versions": dict(STAGE_VERSIONS),
            "failures": failures,
        }
