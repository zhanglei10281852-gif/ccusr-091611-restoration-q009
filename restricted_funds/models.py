"""领域数据模型：器物、预算版本、采购承诺、发票、到货验收、材料用量。"""

from dataclasses import dataclass, field
from decimal import Decimal

from .money import D, q2


@dataclass
class Artifact:
    artifact_id: str
    name: str
    artifact_class: str          # 一级文物 / 二级文物 / 一般文物
    project_id: str = "proj-restoration"


@dataclass
class BudgetVersion:
    """预算版本。新版本总额取代旧版本，但调整只能落在尚未承诺的额度上。"""
    version_id: str
    fund_id: str
    amount: Decimal              # 该版本核定的预算总额（基金币种）
    effective_date: str
    note: str = ""


@dataclass
class CommitmentLine:
    material_id: str
    material_name: str
    material_origin: str         # domestic / imported
    expense_category: str
    quantity: Decimal
    unit_price: Decimal          # 采购订单币种单价


@dataclass
class Commitment:
    commitment_id: str
    fund_id: str
    artifact_ids: list[str]
    business_date: str           # 下单/占用额度日
    currency: str
    lines: list[CommitmentLine]
    emergency: bool = False
    status: str = "approved"     # approved / pending_approval / rejected / expired / completed
    approval_deadline: str | None = None
    approved_at: str | None = None
    invoice_id: str | None = None
    matched_base: Decimal = Decimal("0.00")   # 已核销占用的额度（按各发票锁定汇率折算的本币累计）
    matched_ccy: Decimal = Decimal("0.00")    # 已核销占用的额度（承诺币种）
    base_amount: Decimal = Decimal("0.00")    # 承诺总额按承诺日汇率折算的本币金额
    fx_rate: Decimal = Decimal("1")           # 承诺日锁定汇率（余量估值始终用它）
    delivered_qty: dict = field(default_factory=dict)  # {material_id: 已交付净数量}

    @property
    def amount(self) -> Decimal:
        return q2(sum((D(l.quantity) * D(l.unit_price) for l in self.lines), Decimal(0)))

    @property
    def is_open(self) -> bool:
        """仍占用预算额度的状态（待批也即时占用待审额度）。"""
        return self.status in ("approved", "pending_approval")


@dataclass
class InvoiceLine:
    line_id: str
    commitment_id: str | None
    material_id: str
    material_name: str
    material_origin: str         # domestic / imported
    expense_category: str
    quantity: Decimal
    unit_price: Decimal          # 发票币种单价（不含税）

    @property
    def net(self) -> Decimal:
        return q2(D(self.quantity) * D(self.unit_price))


@dataclass
class Invoice:
    invoice_id: str
    vendor: str
    currency: str
    business_date: str
    gross_amount: Decimal        # 含税总额（核销与并发控制的上限）
    tax_rate: Decimal = Decimal("0")
    lines: list[InvoiceLine] = field(default_factory=list)
    # 无明细行（简单样例）时的默认属性，用于资助条款判定
    default_origin: str = "domestic"
    default_category: str = "restoration_consumable"

    def line(self, line_id: str | None) -> InvoiceLine | None:
        if line_id is None:
            return None
        for line in self.lines:
            if line.line_id == line_id:
                return line
        return None

    @property
    def net_total(self) -> Decimal:
        if not self.lines:
            return None
        return sum((l.net for l in self.lines), Decimal(0))


@dataclass
class Receipt:
    """到货验收。"""
    receipt_id: str
    commitment_id: str
    invoice_id: str
    business_date: str
    accepted: bool
    accepted_quantities: dict[str, Decimal] = field(default_factory=dict)
    note: str = ""


@dataclass
class MaterialUsage:
    """材料在某件器物上的实际用量（领用料单）。"""
    usage_id: str
    invoice_id: str
    artifact_id: str
    material_id: str
    quantity: Decimal
    business_date: str
