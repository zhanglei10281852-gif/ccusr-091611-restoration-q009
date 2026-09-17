"""月底对齐查询：账面余额、已承诺金额与器物实际消耗对齐，并逐笔解释被拒付款。"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .models import OCCUPYING_STATUSES, PaymentStatus


@dataclass
class FundMonthReport:
    fund_id: str
    currency: str
    budget_current: Decimal
    committed_open: Decimal          # 已承诺（含紧急待审与逾期未批）
    actuals: Decimal                 # 已入账消耗（分摊净额，资助币种）
    book_balance: Decimal            # 账面余额 = 预算 - 已入账消耗
    available: Decimal               # 可用额度 = 账面余额 - 已承诺
    paid: Decimal                    # 已付款（资助币种）
    ledger_expense: Decimal          # 账簿费用科目余额（对齐校验）
    consumption_by_artifact: dict    # 器物实际消耗（金额，按分摊净额归集）
    aligned: bool                    # 账面余额 / 承诺 / 消耗 是否对齐
    payments_frozen: bool
    freeze_reason: str | None


@dataclass
class MonthEndReport:
    period: str                      # YYYY-MM
    funds: list[FundMonthReport]
    usage_by_artifact: dict          # 器物实际消耗（实物量）
    rejections: list[dict]           # 本期被拒付款，逐笔附条款依据

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "funds": [
                {
                    "fund_id": f.fund_id,
                    "currency": f.currency,
                    "budget_current": str(f.budget_current),
                    "committed_open": str(f.committed_open),
                    "actuals": str(f.actuals),
                    "book_balance": str(f.book_balance),
                    "available": str(f.available),
                    "paid": str(f.paid),
                    "ledger_expense": str(f.ledger_expense),
                    "consumption_by_artifact": {
                        k: str(v) for k, v in f.consumption_by_artifact.items()
                    },
                    "aligned": f.aligned,
                    "payments_frozen": f.payments_frozen,
                    "freeze_reason": f.freeze_reason,
                }
                for f in self.funds
            ],
            "usage_by_artifact": self.usage_by_artifact,
            "rejections": self.rejections,
        }

    def render_text(self) -> str:
        lines = [f"== 月底对齐报告 {self.period} =="]
        for f in self.funds:
            flag = "对齐" if f.aligned else "【不对齐】"
            lines.append(
                f"[{f.fund_id}] {flag} 预算 {f.budget_current} {f.currency} | "
                f"已承诺 {f.committed_open} | 已消耗 {f.actuals} | "
                f"账面余额 {f.book_balance} | 可用 {f.available} | 已付款 {f.paid}"
            )
            if f.payments_frozen:
                lines.append(f"    付款冻结：{f.freeze_reason}")
            for artifact_id, amount in sorted(f.consumption_by_artifact.items()):
                lines.append(f"    消耗 {artifact_id}: {amount} {f.currency}")
        if self.usage_by_artifact:
            lines.append("-- 器物实际用量 --")
            for artifact_id, usages in sorted(self.usage_by_artifact.items()):
                for u in usages:
                    lines.append(f"    {artifact_id} {u['material']} "
                                 f"{u['quantity']} {u['unit']}（{u['business_date']}）")
        if self.rejections:
            lines.append("-- 本期被拒付款（逐笔条款依据）--")
            for r in self.rejections:
                lines.append(
                    f"    {r['payment_id']} 发票 {r['invoice_id']} -> {r['fund_id']} "
                    f"{r['amount']} {r['currency']}"
                )
                for reason in r["reasons"]:
                    lines.append(f"      原因：{reason}")
                for clause in r["clauses"]:
                    lines.append(f"      条款依据：{clause}")
        else:
            lines.append("-- 本期无被拒付款 --")
        return "\n".join(lines)


def _period_end(period: str) -> date:
    year, month = (int(x) for x in period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def build_month_end_report(service, period: str) -> MonthEndReport:
    """生成月底报告。余额为截至月末的累计口径，被拒付款按本期过滤。"""
    as_of = _period_end(period)
    fund_reports = []
    for fund in service.funds.values():
        budget_versions = [v for b in service.budgets.values() if b.fund_id == fund.fund_id
                           for v in b.versions if v.business_date <= as_of]
        budget_current = budget_versions[-1].amount if budget_versions else Decimal("0")
        committed = sum(
            (c.amount_fund for c in service.commitments.values()
             if c.fund_id == fund.fund_id and c.business_date <= as_of
             and c.status in OCCUPYING_STATUSES),
            Decimal("0"),
        )
        consumption: dict[str, Decimal] = {}
        for a in service.allocations.values():
            if a.fund_id == fund.fund_id and a.business_date <= as_of:
                consumption[a.artifact_id] = consumption.get(a.artifact_id, Decimal("0")) \
                    + a.amount_fund
        for r in service.returns.values():
            if r.business_date > as_of:
                continue
            for line in r.lines:
                if line.fund_id == fund.fund_id:
                    consumption[line.artifact_id] = \
                        consumption.get(line.artifact_id, Decimal("0")) - line.amount_fund
        actuals = sum(consumption.values(), Decimal("0"))
        paid = sum((p.amount_fund for p in service.payments.values()
                    if p.fund_id == fund.fund_id and p.status is PaymentStatus.APPROVED
                    and p.business_date <= as_of and p.amount_fund is not None),
                   Decimal("0"))
        ledger_expense = service.ledger.account_balance(
            "material_expense", fund.fund_id, fund.currency, as_of=as_of)
        book_balance = budget_current - actuals
        aligned = (ledger_expense == actuals
                   and book_balance + actuals == budget_current)
        fund_reports.append(FundMonthReport(
            fund_id=fund.fund_id,
            currency=fund.currency,
            budget_current=budget_current,
            committed_open=committed,
            actuals=actuals,
            book_balance=book_balance,
            available=book_balance - committed,
            paid=paid,
            ledger_expense=ledger_expense,
            consumption_by_artifact=consumption,
            aligned=aligned,
            payments_frozen=fund.payments_frozen,
            freeze_reason=fund.freeze_reason,
        ))
    usage_by_artifact: dict[str, list] = {}
    for u in service.usage_records.values():
        if u.business_date <= as_of:
            usage_by_artifact.setdefault(u.artifact_id, []).append({
                "material": u.material,
                "quantity": str(u.quantity),
                "unit": u.unit,
                "business_date": u.business_date.isoformat(),
            })
    rejections = [
        {
            "payment_id": r.payment_id,
            "invoice_id": r.invoice_id,
            "fund_id": r.fund_id,
            "amount": str(r.amount),
            "currency": r.currency,
            "business_date": r.business_date.isoformat(),
            "reasons": list(r.reasons),
            "clauses": list(r.clauses),
        }
        for r in service.rejections
        if r.business_date.strftime("%Y-%m") == period
    ]
    return MonthEndReport(period=period, funds=fund_reports,
                          usage_by_artifact=usage_by_artifact, rejections=rejections)
