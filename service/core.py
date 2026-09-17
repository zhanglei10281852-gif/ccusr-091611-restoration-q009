"""受限资金管理服务。

把资助条款、预算版本、采购承诺、到货验收、材料实际用量和费用分摊连接起来，
使财务能在付款前判断每一笔支出的合规来源。

核心不变量：
- 预算调整只能影响尚未承诺的额度；
- 退料沿原分摊比例冲回，不生成无来源余额；
- 跨币种金额按业务日锁定汇率；
- 紧急采购先占用待审额度，逾期未批冻结后续付款；
- 同一发票的并发核销不超过含税总额；
- 结账后更正形成反向凭证。
"""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from typing import Optional

from .fx import FxTable
from .ledger import Entry, Ledger, Voucher, period_of
from .models import (
    Allocation,
    Artifact,
    Budget,
    BudgetVersion,
    Commitment,
    CommitmentKind,
    CommitmentStatus,
    Fund,
    Invoice,
    OCCUPYING_STATUSES,
    Payment,
    PaymentRejection,
    PaymentStatus,
    Receipt,
    Restriction,
    ReturnLine,
    ReturnRecord,
    UsageRecord,
)
from .restrictions import evaluate_restrictions


class ServiceError(Exception):
    pass


class DuplicateError(ServiceError):
    pass


class UnknownObjectError(ServiceError):
    pass


class BudgetAdjustmentError(ServiceError):
    pass


class CommitmentError(ServiceError):
    pass


class OverAllocationError(ServiceError):
    pass


class ReturnError(ServiceError):
    pass


def split_pro_rata(total: Decimal, weights: list[Decimal], quant: Decimal) -> list[Decimal]:
    """按权重比例拆分金额，尾差落到权重最大的一行，保证合计精确等于 total。"""
    base = sum(weights)
    if base <= 0:
        raise ServiceError("拆分权重合计必须为正")
    shares = [(total * w / base).quantize(quant) for w in weights]
    diff = total - sum(shares)
    if diff:
        idx = max(range(len(weights)), key=lambda i: weights[i])
        shares[idx] += diff
    return shares


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class RestrictedFundService:
    def __init__(self, contract: dict, fx: Optional[FxTable] = None):
        self.contract = contract
        self.scale = int(contract.get("rounding_scale", 2))
        self._quant = Decimal("1").scaleb(-self.scale)
        self.fx = fx or FxTable()
        self.funds: dict[str, Fund] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.budgets: dict[str, Budget] = {}
        self.commitments: dict[str, Commitment] = {}
        self.receipts: dict[str, Receipt] = {}
        self.usage_records: dict[str, UsageRecord] = {}
        self.invoices: dict[str, Invoice] = {}
        self.allocations: dict[str, Allocation] = {}
        self.returns: dict[str, ReturnRecord] = {}
        self.payments: dict[str, Payment] = {}
        self.rejections: list[PaymentRejection] = []
        self.ledger = Ledger()
        self._invoice_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._voucher_seq = 0

    # ---------------------------------------------------------------- 基础工具

    def money(self, value) -> Decimal:
        return Decimal(str(value)).quantize(self._quant)

    def _next_voucher_id(self) -> str:
        with self._lock:
            self._voucher_seq += 1
            return f"V-{self._voucher_seq:05d}"

    def _invoice_lock(self, invoice_id: str) -> threading.Lock:
        with self._lock:
            return self._invoice_locks.setdefault(invoice_id, threading.Lock())

    def _convert(self, amount: Decimal, currency: str, fund: Fund, day: date):
        """按业务日锁定汇率折算资助币种。"""
        rate = self.fx.rate_on(day, currency, fund.currency)
        return rate, self.money(amount * rate)

    def _post(self, entry_type: str, business_date: date, entries: list[Entry],
              memo: str = "", reversal_of: Optional[str] = None) -> Voucher:
        voucher = Voucher(
            voucher_id=self._next_voucher_id(),
            entry_type=entry_type,
            period=period_of(business_date),
            business_date=business_date,
            entries=entries,
            reversal_of=reversal_of,
            memo=memo,
        )
        return self.ledger.post(voucher)

    # ---------------------------------------------------------------- 主数据

    def register_fund(self, spec: dict) -> Fund:
        if spec["fund_id"] in self.funds:
            raise DuplicateError(f"资助已存在：{spec['fund_id']}")
        restrictions = [
            Restriction(
                restriction_id=r["restriction_id"],
                dimension=r["dimension"],
                rule=dict(r["rule"]),
                clause=r["clause"],
            )
            for r in spec.get("restrictions", [])
        ]
        for r in restrictions:
            if r.dimension not in self.contract.get("restriction_dimensions", []):
                raise ServiceError(f"未知限制维度：{r.dimension}")
        fund = Fund(
            fund_id=spec["fund_id"],
            name=spec.get("name", spec["fund_id"]),
            currency=spec["currency"],
            restrictions=restrictions,
        )
        self.funds[fund.fund_id] = fund
        return fund

    def register_artifact(self, artifact_id: str, artifact_class: str, name: str = "") -> Artifact:
        artifact = Artifact(artifact_id=artifact_id, artifact_class=artifact_class, name=name)
        self.artifacts[artifact_id] = artifact
        return artifact

    # ---------------------------------------------------------------- 预算版本

    def create_budget(self, budget_id: str, fund_id: str, amount,
                      reason: str, business_date) -> Budget:
        if budget_id in self.budgets:
            raise DuplicateError(f"预算已存在：{budget_id}")
        fund = self._fund(fund_id)
        day = _as_date(business_date)
        amount = self.money(amount)
        budget = Budget(budget_id=budget_id, fund_id=fund_id)
        budget.versions.append(BudgetVersion(1, amount, reason, day))
        self.budgets[budget_id] = budget
        self._post("budget", day, [
            Entry("budget_control", fund_id, amount, Decimal("0"), fund.currency, memo=reason),
            Entry("budget_authorization", fund_id, Decimal("0"), amount, fund.currency, memo=reason),
        ], memo=f"预算 {budget_id} v1")
        return budget

    def adjust_budget(self, budget_id: str, new_amount, reason: str, business_date) -> Budget:
        """预算调整只能影响尚未承诺的额度：新总额不得低于已承诺金额。"""
        budget = self._budget(budget_id)
        fund = self._fund(budget.fund_id)
        day = _as_date(business_date)
        new_amount = self.money(new_amount)
        committed = self.committed_amount(fund.fund_id)
        if new_amount < committed:
            raise BudgetAdjustmentError(
                f"预算调整只能影响尚未承诺的额度：{fund.fund_id} 已承诺 {committed} "
                f"{fund.currency}，不能调至 {new_amount}")
        delta = new_amount - budget.current_amount
        budget.versions.append(BudgetVersion(len(budget.versions) + 1, new_amount, reason, day))
        if delta:
            dr_account, cr_account = ("budget_control", "budget_authorization") if delta > 0 \
                else ("budget_authorization", "budget_control")
            self._post("budget", day, [
                Entry(dr_account, fund.fund_id, abs(delta), Decimal("0"), fund.currency, memo=reason),
                Entry(cr_account, fund.fund_id, Decimal("0"), abs(delta), fund.currency, memo=reason),
            ], memo=f"预算 {budget_id} v{len(budget.versions)} 调整")
        return budget

    def committed_amount(self, fund_id: str) -> Decimal:
        """已承诺金额（含紧急采购待审与逾期未批部分），按资助币种。"""
        return sum(
            (c.amount_fund for c in self.commitments.values()
             if c.fund_id == fund_id and c.status in OCCUPYING_STATUSES),
            Decimal("0"),
        )

    # ---------------------------------------------------------------- 采购承诺

    def create_commitment(self, commitment_id: str, budget_id: str, amount, currency: str,
                          business_date, kind: str = "normal",
                          approval_deadline=None, description: str = "") -> Commitment:
        if commitment_id in self.commitments:
            raise DuplicateError(f"承诺已存在：{commitment_id}")
        budget = self._budget(budget_id)
        fund = self._fund(budget.fund_id)
        day = _as_date(business_date)
        kind = CommitmentKind(kind)
        if kind is CommitmentKind.EMERGENCY and approval_deadline is None:
            raise CommitmentError("紧急采购必须设定审批截止日")
        amount = self.money(amount)
        rate, amount_fund = self._convert(amount, currency, fund, day)
        available = budget.current_amount - self.committed_amount(fund.fund_id)
        if amount_fund > available:
            raise CommitmentError(
                f"超出未承诺额度：{fund.fund_id} 可用 {available} {fund.currency}，"
                f"申请 {amount_fund}")
        commitment = Commitment(
            commitment_id=commitment_id,
            budget_id=budget_id,
            fund_id=fund.fund_id,
            amount=amount,
            currency=currency,
            business_date=day,
            fx_rate=rate,
            amount_fund=amount_fund,
            kind=kind,
            status=CommitmentStatus.PENDING if kind is CommitmentKind.EMERGENCY
            else CommitmentStatus.APPROVED,
            approval_deadline=_as_date(approval_deadline) if approval_deadline else None,
            description=description,
        )
        self.commitments[commitment_id] = commitment
        self._post("commitment", day, [
            Entry("commitment_reserve", fund.fund_id, amount_fund, Decimal("0"),
                  fund.currency, memo=description),
            Entry("budget_headroom", fund.fund_id, Decimal("0"), amount_fund,
                  fund.currency, memo=description),
        ], memo=f"采购承诺 {commitment_id}（{kind.value}）")
        return commitment

    def approve_commitment(self, commitment_id: str, approval_date) -> Commitment:
        c = self._commitment(commitment_id)
        if c.status not in (CommitmentStatus.PENDING, CommitmentStatus.EXPIRED):
            raise CommitmentError(f"承诺 {commitment_id} 当前状态 {c.status.value} 不能审批")
        c.status = CommitmentStatus.APPROVED
        self._refresh_freeze(c.fund_id)
        return c

    def reject_commitment(self, commitment_id: str, reason: str = "") -> Commitment:
        c = self._commitment(commitment_id)
        if c.status not in (CommitmentStatus.PENDING, CommitmentStatus.EXPIRED):
            raise CommitmentError(f"承诺 {commitment_id} 当前状态 {c.status.value} 不能驳回")
        c.status = CommitmentStatus.REJECTED
        self._refresh_freeze(c.fund_id)
        return c

    def sweep_emergency_deadlines(self, as_of) -> list[Commitment]:
        """逾期未批的紧急采购转为过期，并冻结所属资助的后续付款。"""
        day = _as_date(as_of)
        expired = []
        for c in self.commitments.values():
            if (c.kind is CommitmentKind.EMERGENCY
                    and c.status is CommitmentStatus.PENDING
                    and c.approval_deadline is not None
                    and c.approval_deadline < day):
                c.status = CommitmentStatus.EXPIRED
                expired.append(c)
                self._refresh_freeze(c.fund_id)
        return expired

    def _refresh_freeze(self, fund_id: str) -> None:
        fund = self._fund(fund_id)
        overdue = [c for c in self.commitments.values()
                   if c.fund_id == fund_id and c.kind is CommitmentKind.EMERGENCY
                   and c.status is CommitmentStatus.EXPIRED]
        if overdue:
            ids = "、".join(c.commitment_id for c in overdue)
            fund.payments_frozen = True
            fund.freeze_reason = f"紧急采购 {ids} 逾期未批，冻结后续付款"
        else:
            fund.payments_frozen = False
            fund.freeze_reason = None

    # ---------------------------------------------------------------- 到货验收与用量

    def record_receipt(self, receipt_id: str, commitment_id: str,
                       lines: list[dict], accepted_date) -> Receipt:
        if receipt_id in self.receipts:
            raise DuplicateError(f"验收单已存在：{receipt_id}")
        commitment = self._commitment(commitment_id)
        if commitment.status is not CommitmentStatus.APPROVED:
            raise CommitmentError(f"承诺 {commitment_id} 未获批，不能验收")
        receipt = Receipt(receipt_id=receipt_id, commitment_id=commitment_id,
                          lines=list(lines), accepted_date=_as_date(accepted_date))
        self.receipts[receipt_id] = receipt
        return receipt

    def record_usage(self, usage_id: str, artifact_id: str, material: str,
                     material_origin: str, quantity, unit: str, business_date) -> UsageRecord:
        if usage_id in self.usage_records:
            raise DuplicateError(f"用量记录已存在：{usage_id}")
        self._artifact(artifact_id)
        record = UsageRecord(
            usage_id=usage_id, artifact_id=artifact_id, material=material,
            material_origin=material_origin, quantity=Decimal(str(quantity)),
            unit=unit, business_date=_as_date(business_date),
        )
        self.usage_records[usage_id] = record
        return record

    # ---------------------------------------------------------------- 发票与分摊

    def register_invoice(self, invoice_id: str, currency: str, gross_amount,
                         business_date, material_origin: str, expense_category: str,
                         commitment_id: Optional[str] = None, description: str = "") -> Invoice:
        if invoice_id in self.invoices:
            raise DuplicateError(f"发票已存在：{invoice_id}")
        if currency not in self.contract.get("currencies", []):
            raise ServiceError(f"未知币种：{currency}")
        if commitment_id is not None:
            self._commitment(commitment_id)
        invoice = Invoice(
            invoice_id=invoice_id, currency=currency,
            gross_amount=self.money(gross_amount),
            business_date=_as_date(business_date),
            material_origin=material_origin, expense_category=expense_category,
            commitment_id=commitment_id, description=description,
        )
        self.invoices[invoice_id] = invoice
        return invoice

    def allocate_invoice(self, invoice_id: str, rows: list[dict]) -> dict:
        """把发票金额分摊到器物与资助来源。

        每张发票一把锁：检查剩余额度与写入在同一临界区内完成，
        并发核销合计不会超过含税总额。分摊行字段以 contract.required_allocation_fields 为准。
        """
        invoice = self._invoice(invoice_id)
        if not rows:
            raise ServiceError("分摊行不能为空")
        required = set(self.contract["required_allocation_fields"])
        lock = self._invoice_lock(invoice_id)
        with lock:
            for row in rows:
                missing = required - set(row)
                if missing:
                    raise ServiceError(f"分摊行缺少字段：{sorted(missing)}")
                if row["invoice_id"] != invoice_id:
                    raise ServiceError(f"分摊行发票号不符：{row['invoice_id']}")
                if row["currency"] != invoice.currency:
                    raise ServiceError(f"分摊行币种 {row['currency']} 与发票 {invoice.currency} 不符")
                if row["allocation_id"] in self.allocations:
                    raise DuplicateError(f"分摊已存在：{row['allocation_id']}")
                self._fund(row["fund_id"])
                self._artifact(row["artifact_id"])
            new_total = sum((self.money(r["amount"]) for r in rows), Decimal("0"))
            remaining = invoice.gross_amount - self._invoice_net_allocated(invoice_id)
            if new_total > remaining:
                raise OverAllocationError(
                    f"发票 {invoice_id} 本次核销 {new_total} 超出可核销余额 {remaining}"
                    f"（含税总额 {invoice.gross_amount}）")
            created = []
            for row in rows:
                fund = self.funds[row["fund_id"]]
                day = _as_date(row["business_date"])
                rate, amount_fund = self._convert(self.money(row["amount"]),
                                                  row["currency"], fund, day)
                created.append(Allocation(
                    allocation_id=row["allocation_id"], invoice_id=invoice_id,
                    artifact_id=row["artifact_id"], fund_id=fund.fund_id,
                    amount=self.money(row["amount"]), currency=row["currency"],
                    business_date=day, fx_rate=rate, amount_fund=amount_fund,
                ))
            voucher_date = max(a.business_date for a in created)
            entries = []
            for a in created:
                entries.append(Entry("material_expense", a.fund_id, a.amount_fund,
                                     Decimal("0"), self.funds[a.fund_id].currency,
                                     artifact_id=a.artifact_id, memo=a.allocation_id))
                entries.append(Entry("payable", a.fund_id, Decimal("0"), a.amount_fund,
                                     self.funds[a.fund_id].currency,
                                     artifact_id=a.artifact_id, memo=a.allocation_id))
            # 先过账后登记：凭证失败（如期间已结账）时不留下无凭证分摊
            voucher = self._post("allocation", voucher_date, entries,
                                 memo=f"发票 {invoice_id} 分摊")
            for a in created:
                self.allocations[a.allocation_id] = a
            warnings = self._allocation_warnings(invoice, created)
        return {"allocations": created, "voucher_id": voucher.voucher_id,
                "compliance_warnings": warnings}

    def _allocation_warnings(self, invoice: Invoice, allocations: list[Allocation]) -> list[dict]:
        """分摊时的合规预检（不拦截）：付款前还会做强制校验。"""
        warnings = []
        for a in allocations:
            fund = self.funds[a.fund_id]
            artifact = self.artifacts[a.artifact_id]
            decision = evaluate_restrictions(fund.restrictions, {
                "material_origin": invoice.material_origin,
                "expense_category": invoice.expense_category,
                "artifact_class": artifact.artifact_class,
                "business_date": a.business_date,
            })
            if not decision.allowed:
                warnings.append({
                    "allocation_id": a.allocation_id,
                    "fund_id": a.fund_id,
                    "clauses": decision.clauses,
                })
        return warnings

    def _invoice_net_allocated(self, invoice_id: str) -> Decimal:
        allocated = sum((a.amount for a in self.allocations.values()
                         if a.invoice_id == invoice_id), Decimal("0"))
        returned = sum((line.amount for r in self.returns.values()
                        if r.invoice_id == invoice_id for line in r.lines), Decimal("0"))
        return allocated - returned

    # ---------------------------------------------------------------- 退料

    def return_material(self, return_id: str, invoice_id: str, amount,
                        business_date, reason: str) -> ReturnRecord:
        """退料沿原分摊比例冲回：每一行都挂到原分摊与资助来源，不生成无来源余额。"""
        if return_id in self.returns:
            raise DuplicateError(f"退料单已存在：{return_id}")
        invoice = self._invoice(invoice_id)
        day = _as_date(business_date)
        amount = self.money(amount)
        if amount <= 0:
            raise ReturnError("退料金额必须为正")
        lock = self._invoice_lock(invoice_id)
        with lock:
            sources = [a for a in self.allocations.values() if a.invoice_id == invoice_id]
            if not sources:
                raise ReturnError(f"发票 {invoice_id} 无分摊记录，退料无来源")
            sources.sort(key=lambda a: a.allocation_id)
            orig_total = sum((a.amount for a in sources), Decimal("0"))
            returned_total = sum((line.amount for r in self.returns.values()
                                  if r.invoice_id == invoice_id for line in r.lines),
                                 Decimal("0"))
            if returned_total + amount > orig_total:
                raise ReturnError(
                    f"退料累计 {returned_total + amount} 超过已分摊总额 {orig_total}，"
                    f"将产生无来源余额")
            shares = split_pro_rata(amount, [a.amount for a in sources], self._quant)
            lines = []
            for alloc, share in zip(sources, shares):
                if share == 0:
                    continue
                lines.append(ReturnLine(
                    allocation_id=alloc.allocation_id,
                    artifact_id=alloc.artifact_id,
                    fund_id=alloc.fund_id,
                    amount=share,
                    amount_fund=self.money(share * alloc.fx_rate),  # 沿原锁定汇率冲回
                ))
            entries = []
            for line in lines:
                ccy = self.funds[line.fund_id].currency
                entries.append(Entry("payable", line.fund_id, line.amount_fund,
                                     Decimal("0"), ccy, artifact_id=line.artifact_id,
                                     memo=return_id))
                entries.append(Entry("material_expense", line.fund_id, Decimal("0"),
                                     line.amount_fund, ccy, artifact_id=line.artifact_id,
                                     memo=return_id))
            voucher = self._post("return", day, entries,
                                 memo=f"发票 {invoice_id} 退料：{reason}")
            record = ReturnRecord(return_id=return_id, invoice_id=invoice_id,
                                  business_date=day, reason=reason,
                                  lines=lines, voucher_id=voucher.voucher_id)
            self.returns[return_id] = record
        return record

    # ---------------------------------------------------------------- 付款

    def request_payment(self, payment_id: str, invoice_id: str, fund_id: str,
                        amount, business_date) -> Payment:
        """付款前合规判断：条款、冻结状态、未付余额任一不满足即拒付并留痕。"""
        if payment_id in self.payments:
            raise DuplicateError(f"付款单已存在：{payment_id}")
        invoice = self._invoice(invoice_id)
        fund = self._fund(fund_id)
        day = _as_date(business_date)
        amount = self.money(amount)
        self.sweep_emergency_deadlines(day)  # 逾期未批先冻结，再判断付款
        lock = self._invoice_lock(invoice_id)
        with lock:
            reasons: list[str] = []
            clauses: list[str] = []
            if fund.payments_frozen:
                reasons.append(fund.freeze_reason or "资助付款已冻结")
            fund_allocs = [a for a in self.allocations.values()
                           if a.invoice_id == invoice_id and a.fund_id == fund_id]
            artifact_classes = {self.artifacts[a.artifact_id].artifact_class
                                for a in fund_allocs}
            decision = evaluate_restrictions(fund.restrictions, {
                "material_origin": invoice.material_origin,
                "expense_category": invoice.expense_category,
                "artifact_class": artifact_classes,
                "business_date": day,
            })
            if not decision.allowed:
                clauses.extend(decision.clauses)
                reasons.extend(f"违反资助条款：{c}" for c in decision.clauses)
            net = self._fund_net_allocated(invoice_id, fund_id)
            if net <= 0:
                reasons.append(f"发票 {invoice_id} 无 {fund_id} 的分摊来源")
            elif amount > net - self._fund_paid(invoice_id, fund_id):
                reasons.append(
                    f"付款 {amount} 超过该资助未付分摊余额 {net - self._fund_paid(invoice_id, fund_id)}")
            try:
                rate, amount_fund = self._convert(amount, invoice.currency, fund, day)
            except Exception:
                rate, amount_fund = None, None
            if reasons:
                payment = Payment(payment_id, invoice_id, fund_id, amount,
                                  invoice.currency, rate, amount_fund, day,
                                  PaymentStatus.REJECTED)
                self.payments[payment_id] = payment
                self.rejections.append(PaymentRejection(
                    payment_id, invoice_id, fund_id, amount, invoice.currency,
                    day, reasons, clauses))
                return payment
            voucher = self._post("payment", day, [
                Entry("payable", fund_id, amount_fund, Decimal("0"), fund.currency,
                      memo=payment_id),
                Entry("cash", fund_id, Decimal("0"), amount_fund, fund.currency,
                      memo=payment_id),
            ], memo=f"发票 {invoice_id} 付款")
            payment = Payment(payment_id, invoice_id, fund_id, amount,
                              invoice.currency, rate, amount_fund, day,
                              PaymentStatus.APPROVED, voucher_id=voucher.voucher_id)
            self.payments[payment_id] = payment
        return payment

    def _fund_net_allocated(self, invoice_id: str, fund_id: str) -> Decimal:
        allocated = sum((a.amount for a in self.allocations.values()
                         if a.invoice_id == invoice_id and a.fund_id == fund_id), Decimal("0"))
        returned = sum((line.amount for r in self.returns.values()
                        if r.invoice_id == invoice_id for line in r.lines
                        if line.fund_id == fund_id), Decimal("0"))
        return allocated - returned

    def _fund_paid(self, invoice_id: str, fund_id: str) -> Decimal:
        return sum((p.amount for p in self.payments.values()
                    if p.invoice_id == invoice_id and p.fund_id == fund_id
                    and p.status is PaymentStatus.APPROVED), Decimal("0"))

    # ---------------------------------------------------------------- 结账与更正

    def close_period(self, period: str) -> None:
        self.ledger.close_period(period)

    def correct_voucher(self, voucher_id: str, reason: str, business_date) -> Voucher:
        """更正只形成反向凭证，原凭证保留不动。"""
        day = _as_date(business_date)
        return self.ledger.reverse(voucher_id, self._next_voucher_id(), reason, day)

    # ---------------------------------------------------------------- 查找

    def _fund(self, fund_id: str) -> Fund:
        try:
            return self.funds[fund_id]
        except KeyError:
            raise UnknownObjectError(f"资助不存在：{fund_id}") from None

    def _budget(self, budget_id: str) -> Budget:
        try:
            return self.budgets[budget_id]
        except KeyError:
            raise UnknownObjectError(f"预算不存在：{budget_id}") from None

    def _commitment(self, commitment_id: str) -> Commitment:
        try:
            return self.commitments[commitment_id]
        except KeyError:
            raise UnknownObjectError(f"承诺不存在：{commitment_id}") from None

    def _invoice(self, invoice_id: str) -> Invoice:
        try:
            return self.invoices[invoice_id]
        except KeyError:
            raise UnknownObjectError(f"发票不存在：{invoice_id}") from None

    def _artifact(self, artifact_id: str) -> Artifact:
        try:
            return self.artifacts[artifact_id]
        except KeyError:
            raise UnknownObjectError(f"器物不存在：{artifact_id}") from None
