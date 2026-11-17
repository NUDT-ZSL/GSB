"""相关性评估：按等级精确率/召回率与平均融合分。"""

from collections import defaultdict

from .io_utils import read_jsonl

STAGE = "evaluation"
GRADES = (1, 2, 3)


def load_qrels(path):
    qrels = defaultdict(dict)
    for row in read_jsonl(path):
        grade = int(row["grade"])
        if grade not in (0, 1, 2, 3):
            raise ValueError(f"qrel grade out of range for {row}")
        qrels[row["query_id"]][row["doc_id"]] = grade
    return dict(qrels)


def evaluate(retrieval_results, qrels):
    """以“预测/真值是否达到某等级”做二分类，逐等级计算微平均指标。"""
    counts = {
        grade: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for grade in GRADES
    }
    fused_scores = []
    judged_pairs = 0
    per_query = {}

    for result in retrieval_results:
        query_id = result["query_id"]
        truth = qrels.get(query_id, {})
        scored_items = result.get("all_candidates", result["candidates"])
        predictions = {item["doc_id"]: item["predicted_grade"] for item in scored_items}
        # 未被检索到的真值文档按预测 0 档计入，保证漏报被完整统计
        all_doc_ids = sorted(set(truth) | set(predictions))
        query_scores = []
        for doc_id in all_doc_ids:
            true_grade = truth.get(doc_id, 0)
            pred_grade = predictions.get(doc_id, 0)
            judged_pairs += 1
            for grade in GRADES:
                true_positive_flag = true_grade >= grade
                pred_positive_flag = pred_grade >= grade
                if true_positive_flag and pred_positive_flag:
                    counts[grade]["tp"] += 1
                elif not true_positive_flag and pred_positive_flag:
                    counts[grade]["fp"] += 1
                elif true_positive_flag and not pred_positive_flag:
                    counts[grade]["fn"] += 1
                else:
                    counts[grade]["tn"] += 1
        for item in scored_items:
            if item["doc_id"] in truth:
                query_scores.append(item["fused_score"])
        if query_scores:
            per_query[query_id] = round(sum(query_scores) / len(query_scores), 9)
            fused_scores.extend(query_scores)

    metrics = {"per_grade": {}, "judged_pairs": judged_pairs}
    for grade in GRADES:
        bucket = counts[grade]
        precision_denom = bucket["tp"] + bucket["fp"]
        recall_denom = bucket["tp"] + bucket["fn"]
        precision = bucket["tp"] / precision_denom if precision_denom else 0.0
        recall = bucket["tp"] / recall_denom if recall_denom else 0.0
        f1_denom = precision + recall
        metrics["per_grade"][f"grade_{grade}"] = {
            "tp": bucket["tp"],
            "fp": bucket["fp"],
            "fn": bucket["fn"],
            "tn": bucket["tn"],
            "precision": round(precision, 9),
            "recall": round(recall, 9),
            "f1": round(2 * precision * recall / f1_denom, 9) if f1_denom else 0.0,
        }
    metrics["average_fused_score"] = (
        round(sum(fused_scores) / len(fused_scores), 9) if fused_scores else 0.0
    )
    metrics["per_query_average_fused_score"] = dict(sorted(per_query.items()))
    return metrics
