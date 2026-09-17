"""端到端演示：抢救改用进口加固剂后的受限资金核销。

场景还原：
1. 专项资助（fund-local-material）条款禁止支付进口材料；
2. 现场抢救改用进口加固剂，发票 inv-2026-91 拆分计入三件器物；
3. 财务在付款前逐笔判断合规来源，被拒付款留痕并附条款依据；
4. 紧急采购逾期未批冻结后续付款，补办审批后解冻；
5. 退料沿原分摊比例冲回；跨币种承诺按业务日锁定汇率；
6. 结账后更正形成反向凭证；月底输出对齐报告。
"""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from service import (  # noqa: E402
    BudgetAdjustmentError,
    FxTable,
    RestrictedFundService,
    build_month_end_report,
)


def load(name):
    return json.loads((root / "examples" / name).read_text(encoding="utf-8"))


def show(title, payload=""):
    print(f"\n### {title}")
    if payload != "":
        print(payload)


def main():
    contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
    svc = RestrictedFundService(contract, FxTable.from_dict(load("fx_rates.json")))

    for fund in load("funds.json")["funds"]:
        svc.register_fund(fund)
    for artifact in load("artifacts.json")["artifacts"]:
        svc.register_artifact(**artifact)
    show("主数据", "资助 3 项（含限制条款）、器物 3 件已登记")

    svc.create_budget("bud-local", "fund-local-material", 200000, "年度预算下达", "2026-09-01")
    svc.create_budget("bud-general", "fund-general", 300000, "年度预算下达", "2026-09-01")
    svc.create_budget("bud-eu", "fund-eu-conservation", 50000, "年度预算下达", "2026-09-01")
    show("预算", "本土专项 200,000 CNY / 一般经费 300,000 CNY / 中欧资助 50,000 EUR")

    svc.create_commitment("cm-001", "bud-local", 50000, "CNY", "2026-09-10",
                          description="计划内国产材料采购")
    urgent = svc.create_commitment("cm-urg-01", "bud-general", 2000, "EUR", "2026-09-16",
                                   kind="emergency", approval_deadline="2026-09-20",
                                   description="抢救改用进口加固剂（紧急采购）")
    show("采购承诺",
         f"cm-001 50,000 CNY（已批）；cm-urg-01 EUR 2,000 按 2026-09-16 锁定汇率 "
         f"{urgent.fx_rate} 折 {urgent.amount_fund} CNY，待审额度已占用，审批截止 2026-09-20")

    try:
        svc.adjust_budget("bud-general", 10000, "试图压减预算", "2026-09-17")
    except BudgetAdjustmentError as exc:
        show("预算调整被拦截（只能影响尚未承诺的额度）", str(exc))
    svc.adjust_budget("bud-general", 320000, "上级追加一般经费", "2026-09-17")
    show("预算调整", "一般经费调整至 320,000 CNY（高于已承诺额度，允许）")

    invoices = {i["invoice_id"]: i for i in load("invoices.json")["invoices"]}
    for spec in invoices.values():
        svc.register_invoice(**spec)
    svc.record_receipt("rc-01", "cm-001",
                       [{"material": "国产清洗耗材", "quantity": 12, "unit": "件"}],
                       "2026-09-14")
    show("发票与验收", "inv-2026-88（国产耗材 12,800）/ inv-2026-91（进口加固剂 9,600，挂紧急承诺）")

    sample = load("allocations.json")
    result = svc.allocate_invoice(sample["invoice"]["invoice_id"], sample["allocations"])
    show("发票 inv-2026-88 分摊",
         f"{len(result['allocations'])} 行，凭证 {result['voucher_id']}；合规预警 {result['compliance_warnings']}")

    split_rows = [
        {"allocation_id": "alloc-91-1", "invoice_id": "inv-2026-91", "artifact_id": "artifact-31",
         "fund_id": "fund-local-material", "amount": 3200.00, "currency": "CNY",
         "business_date": "2026-09-16"},
        {"allocation_id": "alloc-91-2", "invoice_id": "inv-2026-91", "artifact_id": "artifact-44",
         "fund_id": "fund-general", "amount": 3200.00, "currency": "CNY",
         "business_date": "2026-09-16"},
        {"allocation_id": "alloc-91-3", "invoice_id": "inv-2026-91", "artifact_id": "artifact-52",
         "fund_id": "fund-general", "amount": 3200.00, "currency": "CNY",
         "business_date": "2026-09-16"},
    ]
    result = svc.allocate_invoice("inv-2026-91", split_rows)
    show("发票 inv-2026-91 拆分计入三件器物",
         "合规预警：" + json.dumps(result["compliance_warnings"], ensure_ascii=False, indent=2))

    rejected = svc.request_payment("pay-1", "inv-2026-91", "fund-local-material",
                                   3200, "2026-09-18")
    show("付款前合规判断：专项资助付进口材料",
         f"pay-1 状态 {rejected.status.value}\n"
         + "\n".join(f"  条款依据：{c}" for c in svc.rejections[-1].clauses))

    paid = svc.request_payment("pay-2", "inv-2026-91", "fund-general", 6400, "2026-09-18")
    show("改用合规来源", f"pay-2 状态 {paid.status.value}，凭证 {paid.voucher_id}")

    expired = svc.sweep_emergency_deadlines("2026-09-25")
    frozen = svc.request_payment("pay-3", "inv-2026-88", "fund-general", 8000, "2026-09-25")
    show("紧急采购逾期未批",
         f"过期承诺：{[c.commitment_id for c in expired]}\n"
         f"pay-3 状态 {frozen.status.value}：{svc.rejections[-1].reasons[0]}")

    svc.approve_commitment("cm-urg-01", "2026-09-26")
    retried = svc.request_payment("pay-4", "inv-2026-88", "fund-general", 8000, "2026-09-26")
    show("补办审批后解冻", f"pay-4 状态 {retried.status.value}，凭证 {retried.voucher_id}")

    returned = svc.return_material("ret-1", "inv-2026-88", 640, "2026-09-27", "剩余耗材退库")
    show("退料沿原分摊比例冲回（4800:8000 = 3:5）",
         "\n".join(f"  {line.allocation_id} -> {line.fund_id} 冲回 {line.amount} CNY"
                   for line in returned.lines)
         + f"\n  凭证 {returned.voucher_id}，合计 640.00，无无来源余额")

    for u in load("usage.json")["usage"]:
        svc.record_usage(**u)
    show("材料实际用量", "4 条用量记录已登记（进口加固剂 2.0 kg 分三件器物）")

    locked = svc.commitments["cm-urg-01"]
    show("跨币种汇率锁定",
         f"2026-09-30 汇率已更新为 7.90，但 cm-urg-01 仍按业务日锁定 "
         f"{locked.fx_rate}，折算 {locked.amount_fund} CNY 不变")

    svc.close_period("2026-09")
    reversal = svc.correct_voucher(retried.voucher_id, "付款账号有误，冲回重付", "2026-10-05")
    show("结账后更正",
         f"期间 2026-09 已结账；更正形成反向凭证 {reversal.voucher_id}"
         f"（reversal_of={reversal.reversal_of}，期间 {reversal.period}），原凭证保留")

    report = build_month_end_report(svc, "2026-09")
    print()
    print(report.render_text())


if __name__ == "__main__":
    main()
