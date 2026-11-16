"""移进-归约（arc-standard）转移依存解析核心。

动作空间（固定同分优先级）：SHIFT > LEFT_REDUCE > RIGHT_REDUCE > FINISH。
每一步先做步数上限与预算检查（由 ledger 回调决定），再构造特征、点积打分，
在合法动作集合内取最高分；同分时按上述固定顺序决断。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import StepLimitError
from .model import ACTIONS, Token, TransitionModel

SHIFT, LEFT_REDUCE, RIGHT_REDUCE, FINISH = ACTIONS


def _log_softmax(scores: dict) -> dict:
    values = np.array([scores[a] for a in ACTIONS], dtype=np.float64)
    stable = values - np.max(values)
    log_probs = stable - np.log(np.exp(stable).sum())
    return {action: float(log_probs[i]) for i, action in enumerate(ACTIONS)}


@dataclass
class Arc:
    head: int
    dep: int
    relation: str

    def as_dict(self) -> dict:
        return {"head": self.head, "dep": self.dep,
                "relation": self.relation}


@dataclass
class ParseResult:
    sentence_id: str
    status: str
    code: str | None
    steps_used: int
    trace: list[dict] = field(default_factory=list)
    arcs: list[Arc] = field(default_factory=list)
    log_score: float | None = None
    failure: dict | None = None
    quota: int = 0
    step_limit: int = 0
    exceeded_budget: bool = False
    exceeded_step_limit: bool = False

    def sentence_dict(self) -> dict:
        return {
            "sentence_id": self.sentence_id,
            "status": self.status,
            "code": self.code,
            "steps_used": self.steps_used,
            "log_score": self.log_score,
            "arcs": [arc.as_dict() for arc in self.arcs],
            "trace": self.trace,
        }


class DependencyParser:
    """单句确定性解析器；预算申请通过 ledger.request 注入。"""

    def __init__(self, model: TransitionModel, step_limit: int,
                 quota: int, ledger=None):
        self.model = model
        self.step_limit = step_limit
        self.quota = quota
        self.ledger = ledger

    def _legal_actions(self, stack: list[Token],
                       buffer_: list[Token]) -> list[str]:
        legal = []
        if buffer_:
            legal.append(SHIFT)
        # 左归约弹出栈顶次元素；ROOT（index=0）不可作为依存词被弹出
        if len(stack) >= 2 and stack[-2].index != 0:
            legal.append(LEFT_REDUCE)
        # 右归约弹出栈顶元素；栈顶为 ROOT 时只能走 FINISH
        if len(stack) >= 2 and stack[-1].index != 0:
            legal.append(RIGHT_REDUCE)
        if not buffer_ and len(stack) == 1 and stack[0].index == 0:
            legal.append(FINISH)
        return legal

    def parse(self, sentence_id: str, tokens: list[Token]) -> ParseResult:
        stack: list[Token] = [Token(0, "<ROOT>", "ROOT")]
        buffer_: list[Token] = list(tokens)
        arcs: list[Arc] = []
        trace: list[dict] = []
        log_probs_total: list[float] = []
        step_no = 0

        while True:
            # 1) 单句步数上限：超限直接终止，不产出弧
            if step_no >= self.step_limit:
                err = StepLimitError(
                    "超过每句最大解析步数", phase="parse",
                    sentence_id=sentence_id, step_no=step_no + 1)
                return self._fail(sentence_id, trace, err,
                                  exceeded_step_limit=True)

            # 2) 预算申请（全局预算 / 每句配额），由 ledger 抛业务异常
            if self.ledger is not None:
                self.ledger.request(sentence_id, step_no + 1)

            step_no += 1

            # 3) 特征构造与打分（异常向上抛给调度器隔离）
            features = self.model.feature_vector(stack, buffer_)
            scores = self.model.score(features)
            log_probs = _log_softmax(scores)
            legal = self._legal_actions(stack, buffer_)

            best_action, tie_broken = self._choose(scores, legal)
            chosen_score = scores[best_action]
            log_probs_total.append(log_probs[best_action])

            snapshot = {
                "step_no": step_no,
                "sort_key": f"{sentence_id}@{step_no:08d}",
                "action": best_action,
                "score": chosen_score,
                "log_prob": log_probs[best_action],
                "tie_broken": tie_broken,
                "stack": [tok.index for tok in stack],
                "buffer": [tok.index for tok in buffer_],
            }
            trace.append(snapshot)

            # 4) 状态转移
            if best_action == SHIFT:
                stack.append(buffer_.pop(0))
            elif best_action == LEFT_REDUCE:
                dependent = stack[-2]
                head = stack[-1]
                arcs.append(Arc(head.index, dependent.index, "LEFT"))
                stack.pop(-2)
            elif best_action == RIGHT_REDUCE:
                dependent = stack[-1]
                head = stack[-2]
                arcs.append(Arc(head.index, dependent.index, "RIGHT"))
                stack.pop()
            elif best_action == FINISH:
                break

        ordered_arcs = sorted(arcs, key=lambda arc: (arc.dep, arc.head))
        return ParseResult(
            sentence_id=sentence_id,
            status="ok",
            code=None,
            steps_used=step_no,
            trace=trace,
            arcs=ordered_arcs,
            log_score=float(sum(log_probs_total)),
            quota=self.quota,
            step_limit=self.step_limit,
        )

    @staticmethod
    def _choose(scores: dict, legal: list[str]) -> tuple[str, bool]:
        """在合法动作中取最高分；同分按 ACTIONS 固定优先级决断。"""
        best_action = None
        best_score = None
        tie_broken = False
        for action in ACTIONS:
            if action not in legal:
                continue
            value = scores[action]
            if best_action is None or value > best_score:
                best_action = action
                best_score = value
                tie_broken = False
            elif value == best_score:
                # 先出现者（ACTIONS 优先级更高）保留，记录发生过同分
                tie_broken = True
        if best_action is None:
            raise RuntimeError("无合法动作，转移系统进入不可恢复状态")
        return best_action, tie_broken

    @staticmethod
    def _fail(sentence_id, trace, err, *, exceeded_step_limit=False,
              exceeded_budget=False) -> ParseResult:
        return ParseResult(
            sentence_id=sentence_id,
            status="failed",
            code=err.code,
            steps_used=len(trace),
            trace=trace,
            arcs=[],
            log_score=None,
            failure=err.failure_record(),
            exceeded_step_limit=exceeded_step_limit,
            exceeded_budget=exceeded_budget,
        )
