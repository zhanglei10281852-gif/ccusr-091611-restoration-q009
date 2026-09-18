"""端到端剧情：抢救性进口加固剂的受限资金核销（2026 年 9 月）。

运行：python examples/run_scenario.py
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restricted_funds import (
    BudgetVersion,
    Commitment,
    CommitmentLine,
    ComplianceError,
    FXBook,
    Invoice,
    InvoiceLine,
    MaterialUsage,
    Receipt,
    RestrictedFundsService,
)
from restricted_funds.loaders import load_all

EX = ROOT / "examples"


def j(name):
    return json.loads((EX / name).read_text(encoding="utf-8"))


def line(t=""):
    print("─" * 78)
    if t:
        print(t)


def show_reject(tag, err: ComplianceError):
    print(f"  ✗ {tag}：{err.code} —— {err.message}")
    for b in err.bases:
        print(f"      条款依据：{b}")


def build_service():
    fx = FXBook(j("fx_rates.json")["rates_by_date"])
    svc = RestrictedFundsService(fx)
    load_all(svc, j("fx_rates.json"), j("funds.json"), j("artifacts.json"),
             j("budgets.json"), j("commitments.json"), j("invoices.json"),
             j("receipts.json"), j("usages.json"))
    return svc


def main():
    svc = build_service()

    line("【1】付款前合规判断：财务先试把进口加固剂发票核销到“国产材料专项”")
    # 全票 15 单位进口加固剂，按器物用量 5/6/4；先试专项基金
    plan_wrong = {"mat-hardener-import": "fund-special-local"}
    try:
        svc.settle("inv-2026-91", plan_wrong)
    except ComplianceError as e:
        show_reject("整票拒绝", e)

    line("【2】改走一般事业经费：两张发票按材料实际用量自动分摊（含税总额分毫不差）")
    ids88 = svc.settle("inv-2026-88", {"mat-dom-adhesive": "fund-special-local"})
    ids91 = svc.settle("inv-2026-91", {"mat-hardener-import": "fund-general"})
    print(f"  inv-2026-88（国产黏合剂 12,800 CNY）分摊凭证：{ids88}")
    print(f"  inv-2026-91（进口加固剂 1,500 EUR）分摊凭证：{ids91}")
    print(f"  发票净核销校验：88 号 {svc.allocated_ccy('inv-2026-88')} CNY ＝ 含税总额；"
          f"91 号 {svc.allocated_ccy('inv-2026-91')} EUR ＝ 含税总额")

    line("【3】并发核销上限：88 号发票已摊满，再来一笔 100 元必须被拒")
    try:
        svc.settle_partial("inv-2026-88", [{
            "artifact_id": "artifact-31", "fund_id": "fund-special-local",
            "amount": Decimal("100.00"),
            "shares": [{"material_id": "mat-dom-adhesive", "quantity": "0.5",
                        "commitment_id": "po-2026-88", "material_origin": "domestic",
                        "expense_category": "consolidation_material"}]}])
    except ComplianceError as e:
        show_reject("超含税总额", e)

    line("【4】紧急采购在待审追认期内不得付款（09-15，批准截止 09-19）")
    r = svc.pay_invoice("inv-2026-91", "2026-09-15")
    for rej in r["rejected"]:
        print(f"  ✗ 拒付 {rej['fund_id']} {rej['amount_base']} CNY：{rej['code']}")
        for b in rej["bases"]:
            print(f"      条款依据：{b}")

    line("【5】09-18 追认获批；09-19 现场退回 3 单位未启用加固剂（付款前退料）")
    svc.approve_commitment("po-2026-91e", "2026-09-18")
    ret_ids = svc.return_material("inv-2026-91", "mat-hardener-import", Decimal("3"), "2026-09-19")
    print(f"  退料冲回凭证：{len(ret_ids)} 张，沿原 5:6:4 比例冲回器物 31/44/77（100/120/80 EUR）")
    print(f"  91 号发票净核销变为 {svc.allocated_ccy('inv-2026-91')} EUR（1,500 − 300），不产生无来源余额")

    line("【6】09-20 付款：只付退料后净额；EUR 按发票业务日 09-14 汇率 7.88 锁定")
    r88 = svc.pay_invoice("inv-2026-88", "2026-09-20")
    r91 = svc.pay_invoice("inv-2026-91", "2026-09-20")
    for p in r88["paid"] + r91["paid"]:
        print(f"  ✓ 已付 {p['fund_id']}：{p['amount']} {p['currency']} = {p['amount_base']} CNY")
    print("  （09-25 汇率已变为 8.05，但付款凭证锁定 7.88，不按付款日重估）")

    line("【7】预算调整只能影响未承诺额度：一般经费想砍到 10,000 被承诺与支出挡住")
    print(f"  一般经费当前占用：{svc.occupied_base('fund-general')} CNY（含承诺余量 300 EUR）")
    try:
        svc.add_budget_version(BudgetVersion(
            "bv-2026-09cut", "fund-general", Decimal("10000.00"), "2026-09-21", "压缩经费"))
    except ComplianceError as e:
        show_reject("预算调减", e)
    svc.add_budget_version(BudgetVersion(
        "bv-2026-09ok", "fund-general", Decimal("200000.00"), "2026-09-21",
        "仅收回未承诺额度"))
    print("  调整为 200,000 CNY 成功（高于已承诺/已核销占用）")

    line("【8】另一笔紧急采购 po-92e 逾期未批：09-28 付款触发冻结，随后到货退回供应商")
    c92 = Commitment(
        "po-2026-92e", "fund-general", ["artifact-77"], "2026-09-22", "EUR",
        emergency=True, status="pending_approval", approval_deadline="2026-09-26",
        lines=[CommitmentLine("mat-hardener-import", "进口碳纤维加固剂", "imported",
                              "consolidation_material", Decimal("4"), Decimal("100.00"))])
    svc.create_commitment(c92)
    inv92 = Invoice("inv-2026-92", "Europa Conservazione S.r.l.", "EUR", "2026-09-22",
                    Decimal("200.00"), lines=[
                        InvoiceLine("l-92-1", "po-2026-92e", "mat-hardener-import",
                                    "进口碳纤维加固剂", "imported", "consolidation_material",
                                    Decimal("2"), Decimal("100.00"))])
    svc.register_invoice(inv92)
    svc.register_receipt(Receipt("rc-92", "po-2026-92e", "inv-2026-92", "2026-09-22",
                                 True, {"mat-hardener-import": Decimal("2")}))
    svc.register_usage(MaterialUsage("u-92-77", "inv-2026-92", "artifact-77",
                                     "mat-hardener-import", Decimal("2"), "2026-09-22"))
    svc.settle("inv-2026-92", {"mat-hardener-import": "fund-general"})
    before = svc.occupied_base("fund-general")   # 已核销 200 + 未交货待审 200 EUR
    r92 = svc.pay_invoice("inv-2026-92", "2026-09-28")
    for rej in r92["rejected"]:
        print(f"  ✗ 拒付：{rej['code']} —— {rej['message']}")
        for b in rej["bases"]:
            print(f"      条款依据：{b}")
    print(f"  冻结释放未交货部分的待审占用：{before} → {svc.occupied_base('fund-general')} CNY，"
          f"承诺状态 {c92.status}")
    svc.return_material("inv-2026-92", "mat-hardener-import", Decimal("2"), "2026-09-28")
    print("  已到货的 2 单位加固剂原样退回供应商，沿分摊冲回，该发票净核销归 0")

    line("【9】09 期结账：先出 9 月月底对账（账面余额 / 已承诺 / 器物消耗 / 拒付依据）")
    svc.close_period("2026-09")
    report = svc.month_end_report("2026-09")
    for fid, f in report["funds"].items():
        print(f"  ▸ {fid}（{f['name']}）预算版本 {f['budget_version']}")
        print(f"      预算 {f['budget_base']} ＝ 承诺未核销 {f['committed_open_base']}"
              f"（其中待审 {f['pending_approval_base']}）＋ 已核销净额 {f['expensed_base']}"
              f" ＋ 可用 {f['available_base']}  恒等式={f['identity_ok']}")
        if f["frozen_commitments"]:
            print(f"      已冻结承诺：{f['frozen_commitments']}")
    for aid, a in report["artifacts"].items():
        print(f"  ▸ {aid}（{a['name']}）实际净消耗折金 {a['consumed_value_base']} CNY"
              f" ＝ 账面分摊 {a['allocated_base']} CNY  对齐={a['consumption_matches_ledger']}")
        print(f"      分基金来源：{a['by_fund']}")
    for ir in report["invoices"]:
        print(f"  ▸ {ir['invoice_id']}：含税 {ir['gross_amount']} {ir['currency']}，"
              f"净核销 {ir['allocated_net']}，已付 {ir['paid_net']}，全额核销={ir['fully_settled']}")
    print("  本月被拒付款（逐笔条款依据）：")
    for rej in report["rejected_payments"]:
        print(f"    - {rej['business_date']} {rej['invoice_id']} / {rej['fund_id']}：{rej['code']}")
        for b in rej["bases"]:
            print(f"        · {b}")

    line("【10】10 月发现 88 号发票器物 31 分摊有误：已账期禁补记，只能先冲付款、再出反向凭证")
    # 9 月已结账：试图把一笔 1 单位退料补记回 9 月，被期间锁定拦截
    try:
        svc.return_material("inv-2026-91", "mat-hardener-import", Decimal("1"), "2026-09-30")
    except ComplianceError as e:
        show_reject("已结账期补记", e)
    alloc_v = next(v for v in svc.ledger.entries
                   if v.entry_type == "allocation" and v.refs.get("invoice_id") == "inv-2026-88"
                   and v.refs["artifact_id"] == "artifact-31")
    pay_v = next(v for v in svc.ledger.entries
                 if v.entry_type == "payment" and v.refs.get("invoice_id") == "inv-2026-88")
    try:
        svc.reverse_voucher(alloc_v.entry_id, "2026-10-08", "器物归属更正")
    except ComplianceError as e:
        show_reject("未冲付款先冲分摊", e)
    rev_pay = svc.reverse_voucher(pay_v.entry_id, "2026-10-08", "器物归属更正：先红冲付款")
    rev_alloc = svc.reverse_voucher(alloc_v.entry_id, "2026-10-08", "器物归属更正：再红冲分摊")
    for rid in (rev_pay, rev_alloc):
        rv = svc.ledger.get(rid)
        print(f"  反向凭证 {rid}：{rv.amount} {rv.currency} @ {rv.fx_rate}"
              f"（沿用原锁定汇率）冲销 {rv.refs['reverses']}")
    print(f"  原凭证 {pay_v.entry_id}/{alloc_v.entry_id} 均标记 reversed_by；9 月报表不受影响")
    line()
    print("剧情结束：专项资助未支付一分钱进口材料；每笔支出均可追溯到合规基金、承诺与器物用量。")


if __name__ == "__main__":
    main()
