"""把 examples/ 下的 JSON 样例装配为服务对象。"""

from decimal import Decimal

from .models import (
    Artifact,
    BudgetVersion,
    Commitment,
    CommitmentLine,
    Invoice,
    InvoiceLine,
    MaterialUsage,
    Receipt,
)
from .money import D
from .terms import Fund, Term


def load_funds(data: dict) -> list[Fund]:
    funds = []
    for row in data["funds"]:
        terms = [
            Term(dimension=t["dimension"], operator=t["operator"],
                 values=tuple(t["values"]), clause=t.get("clause", ""))
            for t in row.get("terms", [])
        ]
        funds.append(Fund(fund_id=row["fund_id"], name=row["name"],
                          currency=row["currency"], terms=terms))
    return funds


def load_artifacts(data: dict) -> list[Artifact]:
    return [Artifact(**row) for row in data["artifacts"]]


def load_budgets(data: dict) -> list[BudgetVersion]:
    return [BudgetVersion(version_id=r["version_id"], fund_id=r["fund_id"],
                          amount=D(r["amount"]), effective_date=r["effective_date"],
                          note=r.get("note", "")) for r in data["budgets"]]


def load_commitments(data: dict) -> list[Commitment]:
    out = []
    for r in data["commitments"]:
        lines = [CommitmentLine(
            material_id=l["material_id"], material_name=l["material_name"],
            material_origin=l["material_origin"], expense_category=l["expense_category"],
            quantity=D(l["quantity"]), unit_price=D(l["unit_price"])) for l in r["lines"]]
        out.append(Commitment(
            commitment_id=r["commitment_id"], fund_id=r["fund_id"],
            artifact_ids=list(r["artifact_ids"]), business_date=r["business_date"],
            currency=r["currency"], lines=lines, emergency=r.get("emergency", False),
            status=r.get("status", "approved"), approval_deadline=r.get("approval_deadline")))
    return out


def load_invoices(data: dict) -> list[Invoice]:
    out = []
    for r in data["invoices"]:
        lines = [InvoiceLine(
            line_id=l["line_id"], commitment_id=l.get("commitment_id"),
            material_id=l["material_id"], material_name=l["material_name"],
            material_origin=l["material_origin"], expense_category=l["expense_category"],
            quantity=D(l["quantity"]), unit_price=D(l["unit_price"])) for l in r.get("lines", [])]
        out.append(Invoice(
            invoice_id=r["invoice_id"], vendor=r["vendor"], currency=r["currency"],
            business_date=r["business_date"], gross_amount=D(r["gross_amount"]),
            tax_rate=D(r.get("tax_rate", "0")), lines=lines))
    return out


def load_receipts(data: dict) -> list[Receipt]:
    return [Receipt(
        receipt_id=r["receipt_id"], commitment_id=r["commitment_id"],
        invoice_id=r["invoice_id"], business_date=r["business_date"],
        accepted=r["accepted"],
        accepted_quantities={k: D(v) for k, v in r.get("accepted_quantities", {}).items()},
        note=r.get("note", "")) for r in data["receipts"]]


def load_usages(data: dict) -> list[MaterialUsage]:
    return [MaterialUsage(
        usage_id=r["usage_id"], invoice_id=r["invoice_id"], artifact_id=r["artifact_id"],
        material_id=r["material_id"], quantity=D(r["quantity"]),
        business_date=r["business_date"]) for r in data["usages"]]


def load_all(service, fx_data: dict, funds_data: dict, artifacts_data: dict,
             budgets_data: dict, commitments_data: dict, invoices_data: dict,
             receipts_data: dict, usages_data: dict) -> None:
    """按依赖顺序把全套样例装入已构造好的服务。"""
    for fund in load_funds(funds_data):
        service.register_fund(fund)
    for art in load_artifacts(artifacts_data):
        service.register_artifact(art)
    for bv in load_budgets(budgets_data):
        service.add_budget_version(bv)
    for c in load_commitments(commitments_data):
        service.create_commitment(c)
    for inv in load_invoices(invoices_data):
        service.register_invoice(inv)
    for rc in load_receipts(receipts_data):
        service.register_receipt(rc)
    for u in load_usages(usages_data):
        service.register_usage(u)
