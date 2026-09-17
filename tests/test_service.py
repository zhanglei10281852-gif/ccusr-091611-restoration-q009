"""受限资金管理服务的业务规则测试（标准库 unittest，无第三方依赖）。"""
import json
import sys
import threading
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from service import (  # noqa: E402
    BudgetAdjustmentError,
    CommitmentError,
    CommitmentStatus,
    DuplicateError,
    FxError,
    FxTable,
    OverAllocationError,
    PaymentStatus,
    PeriodClosedError,
    RestrictedFundService,
    ReturnError,
    build_month_end_report,
    evaluate_restrictions,
    split_pro_rata,
)
from service.models import Restriction  # noqa: E402

CONTRACT = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))


def make_fx():
    fx = FxTable()
    fx.add_rate(date(2026, 9, 15), "EUR", "CNY", "7.80")
    fx.add_rate(date(2026, 9, 16), "EUR", "CNY", "7.80")
    fx.add_rate(date(2026, 9, 30), "EUR", "CNY", "7.90")
    return fx


def make_service():
    svc = RestrictedFundService(CONTRACT, make_fx())
    svc.register_fund({
        "fund_id": "fund-local", "name": "本土专项", "currency": "CNY",
        "restrictions": [{
            "restriction_id": "r1", "dimension": "material_origin",
            "rule": {"deny": ["imported"]},
            "clause": "《专项协议》第4.2条：不得支付进口材料",
        }],
    })
    svc.register_fund({
        "fund_id": "fund-general", "name": "一般经费", "currency": "CNY",
        "restrictions": [{
            "restriction_id": "r2", "dimension": "valid_period",
            "rule": {"not_before": "2026-01-01", "not_after": "2026-12-31"},
            "clause": "《管理办法》第2.3条：仅限2026年度内支出",
        }],
    })
    svc.register_artifact("artifact-1", "ceramic", "梅瓶")
    svc.register_artifact("artifact-2", "painting", "立轴")
    svc.create_budget("bud-local", "fund-local", 100000, "下达", "2026-09-01")
    svc.create_budget("bud-general", "fund-general", 200000, "下达", "2026-09-01")
    return svc


def add_invoice(svc, invoice_id="inv-1", gross="12800.00", origin="domestic",
                category="consumable", day="2026-09-15"):
    return svc.register_invoice(invoice_id, "CNY", gross, day, origin, category)


def alloc_row(allocation_id, invoice_id, artifact_id, fund_id, amount, day="2026-09-15"):
    return {"allocation_id": allocation_id, "invoice_id": invoice_id,
            "artifact_id": artifact_id, "fund_id": fund_id, "amount": amount,
            "currency": "CNY", "business_date": day}


class TestRestrictions(unittest.TestCase):
    def test_deny_and_allow_and_period(self):
        deny_imported = Restriction("r1", "material_origin", {"deny": ["imported"]}, "条款A")
        decision = evaluate_restrictions([deny_imported], {"material_origin": "imported"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.clauses, ["条款A"])
        self.assertTrue(evaluate_restrictions([deny_imported], {"material_origin": "domestic"}).allowed)

        allow = Restriction("r2", "artifact_class", {"allow": ["ceramic"]}, "条款B")
        self.assertFalse(evaluate_restrictions([allow], {"artifact_class": {"ceramic", "metalwork"}}).allowed)
        self.assertTrue(evaluate_restrictions([allow], {"artifact_class": {"ceramic"}}).allowed)

        period = Restriction("r3", "valid_period",
                             {"not_before": "2026-01-01", "not_after": "2026-12-31"}, "条款C")
        self.assertFalse(evaluate_restrictions(
            [period], {"business_date": date(2027, 1, 5)}).allowed)
        self.assertTrue(evaluate_restrictions(
            [period], {"business_date": date(2026, 6, 1)}).allowed)


class TestFx(unittest.TestCase):
    def test_business_day_lock_and_fallback(self):
        fx = make_fx()
        self.assertEqual(fx.rate_on(date(2026, 9, 15), "EUR", "CNY"), Decimal("7.80"))
        # 当日缺失取此前最近报价
        self.assertEqual(fx.rate_on(date(2026, 9, 20), "EUR", "CNY"), Decimal("7.80"))
        # 反向报价取倒数
        self.assertEqual(fx.rate_on(date(2026, 9, 15), "CNY", "EUR"),
                         (Decimal("1") / Decimal("7.80")).quantize(Decimal("0.00000001")))
        self.assertEqual(fx.rate_on(date(2026, 9, 15), "CNY", "CNY"), Decimal("1"))
        with self.assertRaises(FxError):
            fx.rate_on(date(2026, 9, 15), "USD", "EUR")

    def test_commitment_locks_rate_at_business_date(self):
        svc = make_service()
        c = svc.create_commitment("cm-eur", "bud-general", 1000, "EUR", "2026-09-16")
        self.assertEqual(c.fx_rate, Decimal("7.80"))
        self.assertEqual(c.amount_fund, Decimal("7800.00"))
        # 汇率表后续变化不影响已锁定金额
        svc.fx.add_rate(date(2026, 10, 1), "EUR", "CNY", "8.10")
        self.assertEqual(svc.commitments["cm-eur"].amount_fund, Decimal("7800.00"))


class TestBudget(unittest.TestCase):
    def test_adjustment_only_touches_uncommitted(self):
        svc = make_service()
        svc.create_commitment("cm-1", "bud-general", 60000, "CNY", "2026-09-05")
        with self.assertRaises(BudgetAdjustmentError):
            svc.adjust_budget("bud-general", 59999, "压减", "2026-09-06")
        budget = svc.adjust_budget("bud-general", 60000, "压减到承诺线", "2026-09-06")
        self.assertEqual(budget.current_amount, Decimal("60000.00"))
        budget = svc.adjust_budget("bud-general", 250000, "追加", "2026-09-07")
        self.assertEqual(budget.current_amount, Decimal("250000.00"))
        self.assertEqual(len(budget.versions), 3)  # 初始 + 两次调整，历史保留

    def test_emergency_pending_occupies_quota(self):
        svc = make_service()
        svc.create_commitment("cm-n", "bud-general", 150000, "CNY", "2026-09-05")
        svc.create_commitment("cm-e", "bud-general", 50000, "CNY", "2026-09-06",
                              kind="emergency", approval_deadline="2026-09-10")
        with self.assertRaises(CommitmentError):
            svc.create_commitment("cm-x", "bud-general", 1, "CNY", "2026-09-07")

    def test_emergency_requires_deadline(self):
        svc = make_service()
        with self.assertRaises(CommitmentError):
            svc.create_commitment("cm-e", "bud-general", 100, "CNY", "2026-09-06",
                                  kind="emergency")


class TestInvoiceConcurrency(unittest.TestCase):
    def test_concurrent_writeoff_never_exceeds_gross(self):
        for _ in range(20):
            svc = make_service()
            add_invoice(svc, gross="10000.00")
            barrier = threading.Barrier(2)
            outcomes = []

            def worker(tag):
                barrier.wait()
                try:
                    svc.allocate_invoice("inv-1", [alloc_row(f"a-{tag}", "inv-1",
                                                             "artifact-1", "fund-general", 7000)])
                    outcomes.append("ok")
                except OverAllocationError:
                    outcomes.append("rejected")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(outcomes), ["ok", "rejected"])
            net = svc._invoice_net_allocated("inv-1")
            self.assertEqual(net, Decimal("7000.00"))

    def test_duplicate_allocation_id_rejected(self):
        svc = make_service()
        add_invoice(svc)
        svc.allocate_invoice("inv-1", [alloc_row("a-1", "inv-1", "artifact-1",
                                                 "fund-general", 100)])
        with self.assertRaises(DuplicateError):
            svc.allocate_invoice("inv-1", [alloc_row("a-1", "inv-1", "artifact-2",
                                                     "fund-general", 100)])

    def test_concurrent_payment_never_exceeds_unpaid_balance(self):
        svc = make_service()
        add_invoice(svc, gross="1000.00")
        svc.allocate_invoice("inv-1", [alloc_row("a-1", "inv-1", "artifact-1",
                                                 "fund-general", 1000)])
        barrier = threading.Barrier(2)
        results = []

        def worker(tag):
            barrier.wait()
            payment = svc.request_payment(f"pay-{tag}", "inv-1", "fund-general",
                                          700, "2026-09-16")
            results.append(payment.status)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(s.value for s in results),
                         [PaymentStatus.APPROVED.value, PaymentStatus.REJECTED.value])


class TestReturns(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        add_invoice(self.svc)  # 12800
        self.svc.allocate_invoice("inv-1", [
            alloc_row("a-1", "inv-1", "artifact-1", "fund-local", 4800),
            alloc_row("a-2", "inv-1", "artifact-2", "fund-general", 8000),
        ])

    def test_return_follows_original_ratio(self):
        record = self.svc.return_material("ret-1", "inv-1", 640, "2026-09-20", "退库")
        by_fund = {line.fund_id: line.amount for line in record.lines}
        self.assertEqual(by_fund["fund-local"], Decimal("240.00"))
        self.assertEqual(by_fund["fund-general"], Decimal("400.00"))
        # 每行都挂回原分摊，不产生无来源余额
        for line in record.lines:
            self.assertIn(line.allocation_id, self.svc.allocations)
        self.assertEqual(self.svc._fund_net_allocated("inv-1", "fund-local"),
                         Decimal("4560.00"))
        self.assertEqual(self.svc._fund_net_allocated("inv-1", "fund-general"),
                         Decimal("7600.00"))

    def test_return_cannot_exceed_allocated(self):
        self.svc.return_material("ret-1", "inv-1", 12800, "2026-09-20", "全退")
        with self.assertRaises(ReturnError):
            self.svc.return_material("ret-2", "inv-1", "0.01", "2026-09-21", "超额退料")

    def test_return_without_source_rejected(self):
        svc = make_service()
        add_invoice(svc, invoice_id="inv-empty")
        with self.assertRaises(ReturnError):
            svc.return_material("ret-9", "inv-empty", 100, "2026-09-20", "无来源")

    def test_split_pro_rata_rounding_keeps_total(self):
        quant = Decimal("0.01")
        shares = split_pro_rata(Decimal("100.00"),
                                [Decimal("1"), Decimal("1"), Decimal("1")], quant)
        self.assertEqual(sum(shares), Decimal("100.00"))


class TestPaymentCompliance(unittest.TestCase):
    def test_imported_material_rejected_with_clause(self):
        svc = make_service()
        add_invoice(svc, invoice_id="inv-imp", gross="3200.00", origin="imported",
                    category="material", day="2026-09-16")
        svc.allocate_invoice("inv-imp", [
            alloc_row("a-1", "inv-imp", "artifact-1", "fund-local", 3200, "2026-09-16"),
        ])
        payment = svc.request_payment("pay-1", "inv-imp", "fund-local", 3200, "2026-09-17")
        self.assertEqual(payment.status, PaymentStatus.REJECTED)
        rejection = svc.rejections[-1]
        self.assertTrue(any("第4.2条" in c for c in rejection.clauses))

    def test_compliant_source_pays(self):
        svc = make_service()
        add_invoice(svc, invoice_id="inv-imp", gross="3200.00", origin="imported",
                    category="material", day="2026-09-16")
        svc.allocate_invoice("inv-imp", [
            alloc_row("a-1", "inv-imp", "artifact-1", "fund-general", 3200, "2026-09-16"),
        ])
        payment = svc.request_payment("pay-1", "inv-imp", "fund-general", 3200, "2026-09-17")
        self.assertEqual(payment.status, PaymentStatus.APPROVED)

    def test_valid_period_clause(self):
        svc = make_service()
        add_invoice(svc, invoice_id="inv-late", gross="100.00", day="2026-09-16")
        svc.allocate_invoice("inv-late", [
            alloc_row("a-1", "inv-late", "artifact-1", "fund-general", 100, "2026-09-16"),
        ])
        payment = svc.request_payment("pay-1", "inv-late", "fund-general", 100, "2027-01-10")
        self.assertEqual(payment.status, PaymentStatus.REJECTED)
        self.assertTrue(any("第2.3条" in c for c in svc.rejections[-1].clauses))

    def test_overpayment_rejected(self):
        svc = make_service()
        add_invoice(svc, gross="1000.00")
        svc.allocate_invoice("inv-1", [alloc_row("a-1", "inv-1", "artifact-1",
                                                 "fund-general", 1000)])
        svc.request_payment("pay-1", "inv-1", "fund-general", 800, "2026-09-16")
        payment = svc.request_payment("pay-2", "inv-1", "fund-general", 300, "2026-09-17")
        self.assertEqual(payment.status, PaymentStatus.REJECTED)


class TestEmergencyFreeze(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.svc.create_commitment("cm-urg", "bud-general", 2000, "EUR", "2026-09-16",
                                   kind="emergency", approval_deadline="2026-09-20")
        add_invoice(self.svc, invoice_id="inv-imp", gross="9600.00", origin="imported",
                    category="material", day="2026-09-16")
        self.svc.allocate_invoice("inv-imp", [
            alloc_row("a-1", "inv-imp", "artifact-1", "fund-general", 9600, "2026-09-16"),
        ])

    def test_overdue_emergency_freezes_payments(self):
        payment = self.svc.request_payment("pay-1", "inv-imp", "fund-general",
                                           9600, "2026-09-25")
        self.assertEqual(payment.status, PaymentStatus.REJECTED)
        self.assertTrue(self.svc.funds["fund-general"].payments_frozen)
        self.assertTrue(any("冻结" in r for r in self.svc.rejections[-1].reasons))
        self.assertEqual(self.svc.commitments["cm-urg"].status, CommitmentStatus.EXPIRED)

    def test_payment_before_deadline_not_frozen(self):
        payment = self.svc.request_payment("pay-1", "inv-imp", "fund-general",
                                           9600, "2026-09-19")
        self.assertEqual(payment.status, PaymentStatus.APPROVED)

    def test_late_approval_unfreezes(self):
        self.svc.request_payment("pay-1", "inv-imp", "fund-general", 9600, "2026-09-25")
        self.svc.approve_commitment("cm-urg", "2026-09-26")
        self.assertFalse(self.svc.funds["fund-general"].payments_frozen)
        payment = self.svc.request_payment("pay-2", "inv-imp", "fund-general",
                                           9600, "2026-09-26")
        self.assertEqual(payment.status, PaymentStatus.APPROVED)

    def test_rejection_of_emergency_releases_quota(self):
        self.svc.sweep_emergency_deadlines(date(2026, 9, 25))
        committed_before = self.svc.committed_amount("fund-general")
        self.assertEqual(committed_before, Decimal("15600.00"))  # 2000 EUR * 7.80
        self.svc.reject_commitment("cm-urg", "供应商无法供货")
        self.assertEqual(self.svc.committed_amount("fund-general"), Decimal("0"))
        self.assertFalse(self.svc.funds["fund-general"].payments_frozen)


class TestPeriodClose(unittest.TestCase):
    def test_closed_period_rejects_posting_and_correction_reverses(self):
        svc = make_service()
        add_invoice(svc, gross="1000.00")
        result = svc.allocate_invoice("inv-1", [
            alloc_row("a-1", "inv-1", "artifact-1", "fund-general", 1000),
        ])
        svc.close_period("2026-09")
        # 已结账期间禁止直接入账（业务日落在 2026-09 的分摊凭证会被拒绝）
        with self.assertRaises(PeriodClosedError):
            svc.allocate_invoice("inv-1", [
                alloc_row("a-2", "inv-1", "artifact-2", "fund-general", 0, "2026-09-20"),
            ])
        # 更正形成反向凭证，原凭证保留
        reversal = svc.correct_voucher(result["voucher_id"], "科目用错", "2026-10-05")
        self.assertEqual(reversal.entry_type, "reversal")
        self.assertEqual(reversal.reversal_of, result["voucher_id"])
        self.assertEqual(reversal.period, "2026-10")
        original = svc.ledger.get(result["voucher_id"])
        self.assertEqual(original.entry_type, "allocation")
        balance = svc.ledger.account_balance("material_expense", "fund-general", "CNY")
        self.assertEqual(balance, Decimal("0"))  # 原凭证 + 反向凭证相互抵销


class TestMonthEndReport(unittest.TestCase):
    def test_alignment_and_rejection_explanation(self):
        svc = make_service()
        svc.create_commitment("cm-1", "bud-local", 10000, "CNY", "2026-09-05")
        add_invoice(svc, gross="12800.00")
        svc.allocate_invoice("inv-1", [
            alloc_row("a-1", "inv-1", "artifact-1", "fund-local", 4800),
            alloc_row("a-2", "inv-1", "artifact-2", "fund-general", 8000),
        ])
        svc.return_material("ret-1", "inv-1", 640, "2026-09-20", "退库")
        add_invoice(svc, invoice_id="inv-imp", gross="3200.00", origin="imported",
                    category="material", day="2026-09-16")
        svc.allocate_invoice("inv-imp", [
            alloc_row("a-3", "inv-imp", "artifact-1", "fund-local", 3200, "2026-09-16"),
        ])
        svc.request_payment("pay-1", "inv-imp", "fund-local", 3200, "2026-09-17")  # 被拒
        svc.request_payment("pay-2", "inv-1", "fund-general", 7600, "2026-09-18")  # 退料后净额
        svc.record_usage("use-1", "artifact-1", "加固剂", "imported", "0.8", "kg",
                         "2026-09-17")

        report = build_month_end_report(svc, "2026-09")
        by_fund = {f.fund_id: f for f in report.funds}

        local = by_fund["fund-local"]
        self.assertTrue(local.aligned)
        self.assertEqual(local.actuals, Decimal("4560.00") + Decimal("3200.00"))
        self.assertEqual(local.book_balance,
                         local.budget_current - local.actuals)
        self.assertEqual(local.committed_open, Decimal("10000.00"))
        self.assertEqual(local.consumption_by_artifact["artifact-1"],
                         Decimal("4560.00") + Decimal("3200.00"))

        general = by_fund["fund-general"]
        self.assertTrue(general.aligned)
        self.assertEqual(general.actuals, Decimal("7600.00"))
        self.assertEqual(general.paid, Decimal("7600.00"))
        self.assertEqual(general.consumption_by_artifact["artifact-2"],
                         Decimal("7600.00"))

        # 器物实际消耗合计与各资助已消耗对齐
        total_consumption = sum(
            (amount for f in report.funds for amount in f.consumption_by_artifact.values()),
            Decimal("0"),
        )
        self.assertEqual(total_consumption,
                         sum((f.actuals for f in report.funds), Decimal("0")))

        # 被拒付款逐笔附条款依据
        self.assertEqual(len(report.rejections), 1)
        rejection = report.rejections[0]
        self.assertEqual(rejection["payment_id"], "pay-1")
        self.assertTrue(any("第4.2条" in c for c in rejection["clauses"]))

        # 实物用量进入月底视图
        self.assertIn("artifact-1", report.usage_by_artifact)


if __name__ == "__main__":
    unittest.main()
