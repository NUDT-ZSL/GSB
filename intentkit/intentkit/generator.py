"""本地确定性工单样本与意图配置生成器。

所有随机数来自固定种子的 random.Random，词表受控，
不访问网络、不依赖系统时间，重复执行逐字节一致。
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence, Tuple

from .models import IntentConfig, IntentRule, Ticket

DEFAULT_SEED = 20261123


VOCAB: Dict[str, List[str]] = {
    "refund_request": [
        "我要申请退款",
        "请帮我办理退款",
        "订单如何退款",
        "退款多久能到账",
    ],
    "refund_progress": [
        "查询退款进度",
        "我的退款还在审核吗",
        "退款处理到哪一步了",
        "帮我看下退款状态",
    ],
    "invoice_issue": [
        "发票怎么开",
        "申请开具发票",
        "发票信息填错了怎么办",
        "电子发票多久发送",
    ],
    "logistics_query": [
        "查询物流信息",
        "快递到哪里了",
        "帮我查一下快递单号",
        "物流一直不更新",
    ],
    "account_login": [
        "账号无法登录",
        "登录提示密码错误",
        "验证码收不到怎么办",
        "账号被锁定了",
    ],
    "payment_issue": [
        "支付失败怎么办",
        "扣款了但是订单没生成",
        "银行卡支付被拒绝",
        "重复扣款如何处理",
    ],
    "product_consult": [
        "商品规格有哪些",
        "这款产品支持七天无理由吗",
        "库存什么时候补货",
        "想咨询一下产品功能",
    ],
    "complaint": [
        "我要投诉客服态度",
        "服务体验很差需要投诉",
        "投诉物流配送太慢",
        "对处理结果不满意要投诉",
    ],
    "general_consultation": [
        "你好，我想咨询一个问题",
        "请问人工客服在吗",
        "能帮我转接人工吗",
        "我有个问题想了解",
    ],
    "noise": [
        "今天天气好像还不错",
        "这个系统的界面颜色挺好看",
        "请问你们公司在哪里办公",
        "随便看看，暂时没有具体问题",
    ],
}


KEYWORDS_V1: Dict[str, List[str]] = {
    "refund_request": ["退款", "申请退款", "退货退款", "退钱"],
    "refund_progress": ["退款进度", "退款状态", "审核"],
    "invoice_issue": ["发票", "电子发票", "开票"],
    "logistics_query": ["物流", "快递", "快递单号", "配送"],
    "account_login": ["登录", "验证码", "密码", "账号锁定"],
    "payment_issue": ["支付", "扣款", "银行卡", "重复扣款"],
    "product_consult": ["商品", "产品", "库存", "规格"],
    "complaint": ["投诉", "态度", "不满意"],
    "general_consultation": ["人工客服", "咨询", "转接人工"],
}


CONTROLLED_LABELS_V1: Tuple[str, ...] = (
    "refund_request",
    "refund_progress",
    "invoice_issue",
    "logistics_query",
    "account_login",
    "payment_issue",
    "product_consult",
    "complaint",
    "general_consultation",
)


def build_config_v1() -> IntentConfig:
    """构建版本 1 的完整意图配置。"""
    rules: List[IntentRule] = []
    thresholds = {
        "refund_request": 0.45,
        "refund_progress": 0.45,
        "invoice_issue": 0.45,
        "logistics_query": 0.45,
        "account_login": 0.45,
        "payment_issue": 0.45,
        "product_consult": 0.50,
        "complaint": 0.50,
        "general_consultation": 0.40,
    }
    fallbacks = {
        "refund_progress": "refund_request",
    }
    for label in CONTROLLED_LABELS_V1:
        rules.append(
            IntentRule(
                label=label,
                keywords=tuple(KEYWORDS_V1[label]),
                weight=10 + (5 if label.startswith("refund") else 0),
                threshold=thresholds[label],
                fallback_label=fallbacks.get(label),
                batch_size=64,
            )
        )
    return IntentConfig.build(
        version=1,
        controlled_labels=CONTROLLED_LABELS_V1,
        rules=rules,
        default_fallback_label="general_consultation",
    )


def build_config_v2() -> IntentConfig:
    """版本 2：新增关键词、调整门限，受控集合不变。"""
    config_v1 = build_config_v1()
    rule_by_label = {r.label: r for r in config_v1.rules}
    extra_keywords = {
        "refund_request": ["仅退款"],
        "logistics_query": ["发货"],
        "payment_issue": ["免密支付"],
    }
    rules: List[IntentRule] = []
    for label in CONTROLLED_LABELS_V1:
        rule = rule_by_label[label]
        keywords = tuple(sorted(set(rule.keywords) | set(extra_keywords.get(label, []))))
        threshold = 0.42 if label == "logistics_query" else rule.threshold
        rules.append(
            IntentRule(
                label=label,
                keywords=keywords,
                weight=rule.weight,
                threshold=threshold,
                fallback_label=rule.fallback_label,
                batch_size=rule.batch_size,
            )
        )
    return IntentConfig.build(
        version=2,
        controlled_labels=CONTROLLED_LABELS_V1,
        rules=rules,
        default_fallback_label=config_v1.default_fallback_label,
    )


def build_config_v3() -> IntentConfig:
    """版本 3：受控集合收缩，投诉意图下线以触发回退/退休链路。

    退休意图的关键词规则仍参与匹配（用于识别历史流量），
    但其标签不在受控集合内，命中后必须走回退意图。
    """
    config_v2 = build_config_v2()
    controlled_v3 = tuple(
        label for label in CONTROLLED_LABELS_V1 if label != "complaint"
    )
    rules = [
        IntentRule(
            label=r.label,
            keywords=r.keywords,
            weight=r.weight,
            threshold=r.threshold,
            fallback_label=(
                "general_consultation"
                if r.label == "complaint"
                else r.fallback_label
            ),
            batch_size=r.batch_size,
        )
        for r in config_v2.rules
    ]
    return IntentConfig.build(
        version=3,
        controlled_labels=controlled_v3,
        rules=rules,
        default_fallback_label="general_consultation",
    )


def generate_tickets(
    count: int = 96,
    seed: int = DEFAULT_SEED,
    gold_ratio: float = 0.6,
    noise_ratio: float = 0.12,
    multi_intent_ratio: float = 0.15,
) -> List[Ticket]:
    """生成确定性工单集合。

    标签顺序、随机消费顺序固定；gold 可缺失表示未标注。
    """
    rng = random.Random(seed)
    labels = list(CONTROLLED_LABELS_V1)
    tickets: List[Ticket] = []
    for idx in range(count):
        roll = rng.random()
        ticket_id = f"TK{idx:05d}"
        if roll < noise_ratio:
            query = rng.choice(VOCAB["noise"])
            gold: Optional[str] = None
        elif roll < noise_ratio + multi_intent_ratio:
            chosen = rng.sample(labels, 2)
            parts = [rng.choice(VOCAB[chosen[0]]), rng.choice(VOCAB[chosen[1]])]
            query = "，".join(parts)
            gold = chosen[0] if rng.random() < gold_ratio else None
        else:
            label = labels[idx % len(labels)]
            query = rng.choice(VOCAB[label])
            gold = label if rng.random() < gold_ratio else None
        locale = "zh-CN"
        tickets.append(
            Ticket(
                ticket_id=ticket_id,
                query_text=query,
                locale=locale,
                intent_label_gold=gold,
            )
        )
    return tickets


def correct_gold_label(
    tickets: Sequence[Ticket], ticket_id: str, new_label: Optional[str]
) -> List[Ticket]:
    """返回标注被修正后的新工单列表（不可变更新）。"""
    updated: List[Ticket] = []
    for ticket in tickets:
        if ticket.ticket_id == ticket_id:
            updated.append(
                Ticket(
                    ticket_id=ticket.ticket_id,
                    query_text=ticket.query_text,
                    locale=ticket.locale,
                    intent_label_gold=new_label,
                )
            )
        else:
            updated.append(ticket)
    return updated
