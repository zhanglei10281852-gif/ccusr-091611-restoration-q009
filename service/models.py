"""受限资金领域对象。

账务变化全部以可追溯凭证表达；跨币种金额在业务发生日锁定汇率；
结账后的更正只通过反向凭证表达，原始凭证永不修改。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional


class CommitmentKind(str, Enum):
    NORMAL = "normal"          # 计划内采购承诺
    EMERGENCY = "emergency"    # 紧急采购：先占用待审额度


class CommitmentStatus(str, Enum):
    PENDING = "pending_approval"   # 待审（紧急采购在审批前即占用额度）
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"            # 逾期未批：触发后续付款冻结


#: 仍占用预算额度的承诺状态
OCCUPYING_STATUSES = frozenset({
    CommitmentStatus.PENDING,
    CommitmentStatus.APPROVED,
    CommitmentStatus.EXPIRED,
})


@dataclass(frozen=True)
class Restriction:
    """资助限制条款。clause 为对外解释的条款依据。"""
    restriction_id: str
    dimension: str          # material_origin | artifact_class | expense_category | valid_period
    rule: dict              # {"deny": [...]} / {"allow": [...]} / {"not_before": ..., "not_after": ...}
    clause: str


@dataclass
class Fund:
    fund_id: str
    name: str
    currency: str
    restrictions: list[Restriction] = field(default_factory=list)
    payments_frozen: bool = False
    freeze_reason: Optional[str] = None


@dataclass
class Artifact:
    artifact_id: str
    artifact_class: str
    name: str = ""


@dataclass
class BudgetVersion:
    version: int
    amount: Decimal
    reason: str
    business_date: date


@dataclass
class Budget:
    budget_id: str
    fund_id: str
    versions: list[BudgetVersion] = field(default_factory=list)

    @property
    def current_amount(self) -> Decimal:
        return self.versions[-1].amount


@dataclass
class Commitment:
    commitment_id: str
    budget_id: str
    fund_id: str
    amount: Decimal              # 承诺币种金额
    currency: str
    business_date: date
    fx_rate: Decimal             # 业务日锁定汇率
    amount_fund: Decimal         # 折算资助币种金额
    kind: CommitmentKind
    status: CommitmentStatus
    approval_deadline: Optional[date] = None
    description: str = ""


@dataclass
class Receipt:
    """到货验收。"""
    receipt_id: str
    commitment_id: str
    lines: list[dict]
    accepted_date: date


@dataclass
class UsageRecord:
    """材料实际用量。"""
    usage_id: str
    artifact_id: str
    material: str
    material_origin: str
    quantity: Decimal
    unit: str
    business_date: date


@dataclass
class Invoice:
    invoice_id: str
    currency: str
    gross_amount: Decimal        # 含税总额
    business_date: date
    material_origin: str
    expense_category: str
    commitment_id: Optional[str] = None
    description: str = ""


@dataclass
class Allocation:
    """费用分摊：把发票金额拆到器物与资助来源。"""
    allocation_id: str
    invoice_id: str
    artifact_id: str
    fund_id: str
    amount: Decimal              # 发票币种
    currency: str
    business_date: date
    fx_rate: Decimal             # 业务日锁定汇率
    amount_fund: Decimal         # 折算资助币种


@dataclass
class ReturnLine:
    """退料冲回行：始终挂到原分摊，不产生无来源余额。"""
    allocation_id: str
    artifact_id: str
    fund_id: str
    amount: Decimal
    amount_fund: Decimal


@dataclass
class ReturnRecord:
    return_id: str
    invoice_id: str
    business_date: date
    reason: str
    lines: list[ReturnLine]
    voucher_id: str


class PaymentStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class Payment:
    payment_id: str
    invoice_id: str
    fund_id: str
    amount: Decimal
    currency: str
    fx_rate: Optional[Decimal]
    amount_fund: Optional[Decimal]
    business_date: date
    status: PaymentStatus
    voucher_id: Optional[str] = None


@dataclass
class PaymentRejection:
    """被拒付款：逐笔保留条款依据，供月底解释。"""
    payment_id: str
    invoice_id: str
    fund_id: str
    amount: Decimal
    currency: str
    business_date: date
    reasons: list[str]
    clauses: list[str]
