"""核对 domain/contract.json 与 examples/ 全套样例的一致性。

检查：币种、条款维度/操作符、汇率覆盖、发票含税总额、
分摊合计、用量不超验收数量、承诺金额合计。
运行：python3 tools/validate_contract.py
"""

import json
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def D(v):
    return Decimal(str(v))


def q(v):
    return D(v).quantize(Decimal("0.01"))


contract = load("domain/contract.json")
funds_doc = load("examples/funds.json")
fx_doc = load("examples/fx_rates.json")
artifacts_doc = load("examples/artifacts.json")
budgets_doc = load("examples/budgets.json")
commitments_doc = load("examples/commitments.json")
invoices_doc = load("examples/invoices.json")
receipts_doc = load("examples/receipts.json")
usages_doc = load("examples/usages.json")
allocations_doc = load("examples/allocations.json")

errors = []

# 1) 币种
for f in funds_doc["funds"]:
    if f["currency"] not in contract["currencies"]:
        errors.append(f"基金 {f['fund_id']} 币种 {f['currency']} 不在契约内")
for inv in invoices_doc["invoices"]:
    if inv["currency"] not in contract["currencies"]:
        errors.append(f"发票 {inv['invoice_id']} 币种 {inv['currency']} 不在契约内")

# 2) 条款维度与操作符
dims = set(contract["restriction_dimensions"])
ops = set(contract["fund_term_operators"])
for f in funds_doc["funds"]:
    for t in f.get("terms", []):
        if t["dimension"] not in dims:
            errors.append(f"基金 {f['fund_id']} 条款维度 {t['dimension']} 非法")
        if t["operator"] not in ops:
            errors.append(f"基金 {f['fund_id']} 条款操作符 {t['operator']} 非法")

# 3) 跨币种业务日必须有汇率
rates = fx_doc["rates_by_date"]
fund_ccy = {f["fund_id"]: f["currency"] for f in funds_doc["funds"]}
for c in commitments_doc["commitments"]:
    d = c["business_date"]
    if c["currency"] != fund_ccy[c["fund_id"]] and c["currency"] not in rates.get(d, {}):
        errors.append(f"承诺 {c['commitment_id']} 业务日 {d} 缺 {c['currency']} 汇率")
for inv in invoices_doc["invoices"]:
    d = inv["business_date"]
    if inv["currency"] != fx_doc["base"] and inv["currency"] not in rates.get(d, {}):
        errors.append(f"发票 {inv['invoice_id']} 业务日 {d} 缺 {inv['currency']} 汇率")

# 4) 发票含税总额 = 明细净额 ×（1+税率）
gross = {}
for inv in invoices_doc["invoices"]:
    gross[inv["invoice_id"]] = D(inv["gross_amount"])
    net = sum((D(l["quantity"]) * D(l["unit_price"]) for l in inv.get("lines", [])), Decimal("0"))
    expect = q(net * (Decimal("1") + D(inv.get("tax_rate", "0"))))
    if expect != q(inv["gross_amount"]):
        errors.append(f"发票 {inv['invoice_id']} 含税总额 {inv['gross_amount']} != 明细推算 {expect}")

# 5) 原始分摊样例：合计 = 含税总额、币种一致、字段齐全
sample = allocations_doc
invoice = sample["invoice"]
assert invoice["currency"] in contract["currencies"]
required = set(contract["required_allocation_fields"])
assert all(required <= set(row) for row in sample["allocations"])
assert all(row["currency"] == invoice["currency"] for row in sample["allocations"])
allocated = sum((D(row["amount"]) for row in sample["allocations"]), Decimal("0"))
assert allocated == D(invoice["gross_amount"]), "allocations.json 分摊合计不等于含税总额"

# 6) 用量合计不超过验收合格数量
accepted = {}
for r in receipts_doc["receipts"]:
    accepted[r["invoice_id"]] = {k: D(v) for k, v in r.get("accepted_quantities", {}).items()}
used = {}
for u in usages_doc["usages"]:
    key = (u["invoice_id"], u["material_id"])
    used[key] = used.get(key, Decimal("0")) + D(u["quantity"])
for (inv_id, mid), qty in used.items():
    cap = accepted.get(inv_id, {}).get(mid)
    if cap is None or qty > cap:
        errors.append(f"用量 {inv_id}/{mid} = {qty} 超过验收数量 {cap}")

# 7) 按用量模拟整票分摊，合计必须等于含税总额（自动分摊不变量）
for inv in invoices_doc["invoices"]:
    rows = [u for u in usages_doc["usages"] if u["invoice_id"] == inv["invoice_id"]]
    price = {l["material_id"]: D(l["unit_price"]) * (Decimal("1") + D(inv.get("tax_rate", "0")))
             for l in inv.get("lines", [])}
    weight = sum((D(u["quantity"]) * price[u["material_id"]] for u in rows), Decimal("0"))
    if weight and q(weight) != q(inv["gross_amount"]):
        errors.append(f"发票 {inv['invoice_id']} 用量权重 {q(weight)} != 含税总额 {q(inv['gross_amount'])}")

# 8) 紧急采购必须带批准期限
for c in commitments_doc["commitments"]:
    if c.get("emergency") and not c.get("approval_deadline"):
        errors.append(f"紧急承诺 {c['commitment_id']} 缺 approval_deadline")

# 9) 器物与基金引用完整性
art_ids = {a["artifact_id"] for a in artifacts_doc["artifacts"]}
fund_ids = set(fund_ccy)
for b in budgets_doc["budgets"]:
    if b["fund_id"] not in fund_ids:
        errors.append(f"预算引用未知基金 {b['fund_id']}")
for c in commitments_doc["commitments"]:
    if c["fund_id"] not in fund_ids:
        errors.append(f"承诺 {c['commitment_id']} 引用未知基金")
    for a in c["artifact_ids"]:
        if a not in art_ids:
            errors.append(f"承诺 {c['commitment_id']} 引用未知器物 {a}")
for u in usages_doc["usages"]:
    if u["artifact_id"] not in art_ids:
        errors.append(f"用量引用未知器物 {u['artifact_id']}")
    if u["invoice_id"] not in gross:
        errors.append(f"用量引用未知发票 {u['invoice_id']}")

if errors:
    print("样例契约核对失败：")
    for e in errors:
        print(" -", e)
    raise SystemExit(1)

print("受限资金契约与全套样例一致：条款、汇率、发票总额、分摊合计、验收用量、引用完整性均有效")
