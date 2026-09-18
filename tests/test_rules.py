"""受限资金核销规则测试：每条业务规则一组用例。

运行：python -m unittest discover -s tests -v
"""

import sys
import unittest
from decimal import Decimal

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from restricted_funds import (
    Artifact,
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
from restricted_funds.money import q2
from restricted_funds.terms import Fund, Term

DOMESTIC_TERM = "专项协议第3条：禁止支付进口材料"
FREEZE_CLAUSE = "《紧急采购管理办法》第6条：紧急采购须在批准期限内完成追认审批，逾期未批的，冻结后续一切付款"

RATES = {
    "2026-09-10": {"EUR": "7.80", "USD": "6.90"},
    "2026-09-12": {"EUR": "7.90", "USD": "6.95"},
    "2026-09-20": {"EUR": "8.10", "USD": "7.05"},
    "2026-09-30": {"EUR": "8.00", "USD": "7.00"},
    "2026-10-08": {"EUR": "8.20", "USD": "7.10"},
}


def line(mid="m-hardener", origin="imported", price="100.00", qty="10"):
    return InvoiceLine("l1", "po-1", mid, "加固剂", origin,
                       "consolidation_material", Decimal(qty), Decimal(price))


class World:
    """最小测试世界：专项基金（禁进口）+ 一般经费 + 一件器物。"""

    def __init__(self, emergency=False, deadline=None, status="approved"):
        self.svc = RestrictedFundsService(FXBook(RATES))
        self.special = Fund("fund-special", "专项", "CNY", terms=[
            Term("material_origin", "not_in", ("imported",), DOMESTIC_TERM)])
        self.general = Fund("fund-general", "一般经费", "CNY")
        self.svc.register_fund(self.special)
        self.svc.register_fund(self.general)
        self.svc.register_artifact(Artifact("a-1", "器物一", "二级文物"))
        self.svc.register_artifact(Artifact("a-2", "器物二", "一级文物"))
        self.svc.add_budget_version(BudgetVersion("bv1", "fund-special", Decimal("100000"), "2026-01-01"))
        self.svc.add_budget_version(BudgetVersion("bv1", "fund-general", Decimal("100000"), "2026-01-01"))
        self.emergency = emergency
        self.deadline = deadline
        self.status = status

    def commitment(self, fund_id="fund-general", origin="imported", amount_ccy="EUR",
                   qty="10", price="100.00", cid="po-1", date="2026-09-10"):
        c = Commitment(
            cid, fund_id, ["a-1", "a-2"], date, amount_ccy,
            emergency=self.emergency, status=self.status, approval_deadline=self.deadline,
            lines=[CommitmentLine("m-hardener", "加固剂", origin,
                                  "consolidation_material", Decimal(qty), Decimal(price))])
        self.svc.create_commitment(c)
        return c

    def invoice(self, gross="1000.00", ccy="EUR", date="2026-09-12", iid="inv-1",
                qty="10", price="100.00", origin="imported", cid="po-1"):
        inv = Invoice(iid, "海外供应商", ccy, date, Decimal(gross),
                      lines=[InvoiceLine("l1", cid, "m-hardener", "加固剂", origin,
                                         "consolidation_material", Decimal(qty), Decimal(price))])
        self.svc.register_invoice(inv)
        self.svc.register_receipt(Receipt("rc-1", cid, iid, date, True,
                                          {"m-hardener": Decimal(qty)}))
        return inv

    def usage(self, iid="inv-1", split=("5", "5")):
        self.svc.register_usage(MaterialUsage("u1", iid, "a-1", "m-hardener",
                                              Decimal(split[0]), "2026-09-12"))
        self.svc.register_usage(MaterialUsage("u2", iid, "a-2", "m-hardener",
                                              Decimal(split[1]), "2026-09-12"))


class TestFundTerms(unittest.TestCase):
    def test_imported_commitment_blocked_by_special_fund(self):
        w = World()
        with self.assertRaises(ComplianceError) as cm:
            w.commitment("fund-special", "imported")
        self.assertEqual(cm.exception.code, "TERM_DENIED")
        self.assertIn(DOMESTIC_TERM, cm.exception.bases)

    def test_domestic_commitment_allowed(self):
        w = World()
        w.commitment("fund-special", "domestic", "CNY", qty="10", price="100.00")
        self.assertEqual(w.svc.occupied_base("fund-special"), Decimal("1000.00"))

    def test_settle_to_wrong_fund_rejected_with_clause(self):
        w = World()
        w.commitment("fund-general")
        w.invoice()
        w.usage()
        with self.assertRaises(ComplianceError) as cm:
            w.svc.settle("inv-1", {"m-hardener": "fund-special"})
        self.assertEqual(cm.exception.code, "TERM_DENIED")
        self.assertTrue(cm.exception.bases)

    def test_valid_period_term(self):
        svc = RestrictedFundsService(FXBook(RATES))
        f = Fund("f", "限期基金", "CNY", terms=[
            Term("valid_period", "within", ("2026-01-01", "2026-06-30"), "仅限上半年")])
        svc.register_fund(f)
        svc.register_artifact(Artifact("a-1", "x", "一般文物"))
        svc.add_budget_version(BudgetVersion("bv", "f", Decimal("1000"), "2026-01-01"))
        c = Commitment("p", "f", ["a-1"], "2026-09-10", "CNY",
                       lines=[CommitmentLine("m", "料", "domestic", "cat", Decimal("1"), Decimal("10"))])
        with self.assertRaises(ComplianceError) as cm:
            svc.create_commitment(c)
        self.assertIn("仅限上半年", cm.exception.bases)


class TestBudgetVersions(unittest.TestCase):
    def test_adjustment_cannot_touch_committed(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR", qty="10", price="100.00")
        occupied = w.svc.occupied_base("fund-general")   # 1000 EUR × 7.80
        self.assertEqual(occupied, Decimal("7800.00"))
        with self.assertRaises(ComplianceError) as cm:
            w.svc.add_budget_version(BudgetVersion("bv2", "fund-general", Decimal("5000"), "2026-09-11"))
        self.assertEqual(cm.exception.code, "COMMITMENT_ADJUSTMENT_BLOCKED")

    def test_adjustment_above_occupied_succeeds_and_frees_uncommitted(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR", qty="10", price="100.00")
        w.svc.add_budget_version(BudgetVersion("bv2", "fund-general", Decimal("50000"), "2026-09-11"))
        rpt = w.svc.month_end_report("2026-09")
        f = rpt["funds"]["fund-general"]
        self.assertEqual(f["budget_base"], Decimal("50000.00"))
        self.assertEqual(f["committed_open_base"], Decimal("7800.00"))
        self.assertEqual(f["available_base"], Decimal("42200.00"))


class TestEmergency(unittest.TestCase):
    def test_pending_blocks_payment_approve_allows(self):
        w = World(emergency=True, deadline="2026-09-19", status="pending_approval")
        c = w.commitment(date="2026-09-12")
        w.invoice(date="2026-09-12")
        w.usage()
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        r = w.svc.pay_invoice("inv-1", "2026-09-15")
        self.assertEqual(r["paid"], [])
        self.assertEqual(r["rejected"][0]["code"], "COMMITMENT_NOT_APPROVED")
        # 待审期间已占用额度
        self.assertGreater(w.svc.occupied_base("fund-general"), Decimal("7000.00"))
        w.svc.approve_commitment(c.commitment_id, "2026-09-18")
        r = w.svc.pay_invoice("inv-1", "2026-09-18")
        self.assertEqual(len(r["paid"]), 1)

    def test_overdue_freezes_future_payments_and_releases_hold(self):
        w = World(emergency=True, deadline="2026-09-19", status="pending_approval")
        c = w.commitment(date="2026-09-12", qty="10")
        w.invoice(gross="600.00", date="2026-09-12", qty="6")   # 紧急订单部分到货 6/10
        w.usage(split=("3", "3"))
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        held = w.svc.occupied_base("fund-general")   # 已核销 4740 + 余量 4 瓶 3160
        self.assertEqual(held, Decimal("7900.00"))
        r = w.svc.pay_invoice("inv-1", "2026-09-25")
        self.assertEqual(r["rejected"][0]["code"], "EMERGENCY_OVERDUE_FREEZE")
        self.assertIn(FREEZE_CLAUSE, r["rejected"][0]["bases"])
        # 承诺冻结；未交货部分的待审占用释放，已到货部分仍挂费用
        self.assertEqual(c.status, "expired")
        self.assertEqual(w.svc.occupied_base("fund-general"), Decimal("4740.00"))
        # 再次付款依旧被冻结规则拦截
        r2 = w.svc.pay_invoice("inv-1", "2026-09-26")
        self.assertEqual(r2["rejected"][0]["code"], "EMERGENCY_OVERDUE_FREEZE")

    def test_late_approval_attempt_freezes(self):
        w = World(emergency=True, deadline="2026-09-19", status="pending_approval")
        c = w.commitment(date="2026-09-12")
        with self.assertRaises(ComplianceError) as cm:
            w.svc.approve_commitment(c.commitment_id, "2026-09-25")
        self.assertEqual(cm.exception.code, "EMERGENCY_OVERDUE_FREEZE")
        self.assertEqual(c.status, "expired")


class TestConcurrentSettlement(unittest.TestCase):
    def _settled_world(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice()
        w.usage(split=("5", "5"))
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        return w

    def test_total_allocations_cannot_exceed_gross(self):
        w = self._settled_world()
        share = {"material_id": "m-hardener", "quantity": "1", "commitment_id": "po-1",
                 "material_origin": "imported", "expense_category": "consolidation_material"}
        with self.assertRaises(ComplianceError) as cm:
            w.svc.settle_partial("inv-1", [
                {"artifact_id": "a-1", "fund_id": "fund-general",
                 "amount": Decimal("50.00"), "shares": [share]},
                {"artifact_id": "a-2", "fund_id": "fund-general",
                 "amount": Decimal("60.00"), "shares": [share]}])
        self.assertEqual(cm.exception.code, "INVOICE_GROSS_EXCEEDED")

    def test_partial_settlements_sum_to_gross(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice(gross="1000.00")
        w.usage(split=("3", "7"))
        share = {"material_id": "m-hardener", "quantity": "3", "commitment_id": "po-1",
                 "material_origin": "imported", "expense_category": "consolidation_material"}
        w.svc.settle_partial("inv-1", [
            {"artifact_id": "a-1", "fund_id": "fund-general",
             "amount": Decimal("300.00"), "shares": [share]}])
        share2 = dict(share, quantity="7")
        w.svc.settle_partial("inv-1", [
            {"artifact_id": "a-2", "fund_id": "fund-general",
             "amount": Decimal("700.00"), "shares": [share2]}])
        self.assertEqual(w.svc.allocated_ccy("inv-1"), Decimal("1000.00"))

    def test_concurrent_threads_never_exceed_gross(self):
        import threading
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice()
        w.usage(split=("5", "5"))
        share = {"material_id": "m-hardener", "quantity": "5", "commitment_id": "po-1",
                 "material_origin": "imported", "expense_category": "consolidation_material"}
        errors = []

        def worker():
            try:
                w.svc.settle_partial("inv-1", [
                    {"artifact_id": "a-1", "fund_id": "fund-general",
                     "amount": Decimal("600.00"), "shares": [share]}])
            except ComplianceError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(w.svc.allocated_ccy("inv-1"), Decimal("1000.00"))
        self.assertGreaterEqual(errors.count("INVOICE_GROSS_EXCEEDED"), 2)


class TestReturns(unittest.TestCase):
    def test_return_follows_original_ratio(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice()                 # 1000 EUR，用量 5/5
        w.usage(split=("5", "5"))
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        ids = w.svc.return_material("inv-1", "m-hardener", Decimal("4"), "2026-09-13")
        self.assertEqual(len(ids), 2)
        returns = [v for v in w.svc.ledger.entries if v.entry_type == "return"]
        amounts = sorted(v.amount for v in returns)   # 各冲回 200
        self.assertEqual(amounts, [Decimal("-200.00"), Decimal("-200.00")])
        # 净核销 = 600；器物账面各承担 300
        self.assertEqual(w.svc.allocated_ccy("inv-1"), Decimal("600.00"))
        rpt = w.svc.month_end_report("2026-09")
        vals = {a: rpt["artifacts"][a]["allocated_base"] for a in ("a-1", "a-2")}
        self.assertEqual(set(vals.values()), {q2(300 * 7.90)})

    def test_return_cannot_exceed_usage(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice()
        w.usage(split=("5", "5"))
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        with self.assertRaises(ComplianceError):
            w.svc.return_material("inv-1", "m-hardener", Decimal("11"), "2026-09-13")

    def test_no_orphan_balance_after_full_return(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice()
        w.usage(split=("5", "5"))
        before_budget = w.svc.budget_total_base("fund-general")
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        w.svc.return_material("inv-1", "m-hardener", Decimal("10"), "2026-09-13")
        # 费用净额归零，承诺恢复未结，占用回到仅有承诺余量；预算总额不变
        self.assertEqual(w.svc.expensed_base("fund-general"), Decimal("0.00"))
        self.assertEqual(w.svc.budget_total_base("fund-general"), before_budget)


class TestFXLock(unittest.TestCase):
    def test_payment_uses_invoice_business_date_rate(self):
        w = World(emergency=True, deadline="2026-09-19", status="pending_approval")
        c = w.commitment(date="2026-09-12")
        w.invoice(date="2026-09-12")            # EUR 发票日汇率 7.90
        w.usage()
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        w.svc.approve_commitment(c.commitment_id, "2026-09-18")
        r = w.svc.pay_invoice("inv-1", "2026-09-20")   # 付款日汇率 8.10
        pay = r["paid"][0]
        self.assertEqual(pay["amount_base"], q2(1000 * 7.90))   # 不按 8.10
        voucher = w.svc.ledger.get(pay["entry_id"])
        self.assertEqual(voucher.fx_rate, Decimal("7.90"))
        self.assertEqual(voucher.refs["fx_locked_from"], "2026-09-12")


class TestPeriodClose(unittest.TestCase):
    def test_closed_period_rejects_posting_and_reversal_uses_locked_rate(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice(date="2026-09-12")
        w.usage()
        share5 = {"material_id": "m-hardener", "quantity": "5", "commitment_id": "po-1",
                  "material_origin": "imported", "expense_category": "consolidation_material"}
        ids = w.svc.settle_partial("inv-1", [
            {"artifact_id": "a-1", "fund_id": "fund-general",
             "amount": Decimal("500.00"), "shares": [share5]}])
        w.svc.close_period("2026-09")
        # 发票仍有 500 未摊（总额检查通过），但 9 月已结账，必须拒绝补记
        with self.assertRaises(ComplianceError) as cm:
            w.svc.settle_partial("inv-1", business_date="2026-09-30", items=[
                {"artifact_id": "a-2", "fund_id": "fund-general",
                 "amount": Decimal("500.00"), "shares": [share5]}])
        self.assertEqual(cm.exception.code, "PERIOD_CLOSED")
        rev_id = w.svc.reverse_voucher(ids[0], "2026-10-08", "更正")
        rev = w.svc.ledger.get(rev_id)
        self.assertEqual(rev.business_date, "2026-10-08")
        self.assertEqual(rev.fx_rate, Decimal("7.90"))     # 沿用原锁定汇率
        self.assertEqual(rev.amount_base, -w.svc.ledger.get(ids[0]).amount_base)
        self.assertEqual(w.svc.ledger.get(ids[0]).reversed_by, rev_id)


class TestReceiptAndUsage(unittest.TestCase):
    def test_usage_cannot_exceed_accepted_quantity(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.invoice(qty="10")
        w.usage(split=("6", "5"))       # 共 11 > 验收 10
        with self.assertRaises(ComplianceError) as cm:
            w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        self.assertEqual(cm.exception.code, "RECEIPT_NOT_ACCEPTED")

    def test_invoice_without_receipt_cannot_settle(self):
        w = World()
        w.commitment("fund-general", "imported", "EUR")
        w.svc.register_invoice(Invoice("inv-x", "v", "EUR", "2026-09-12", Decimal("1000"),
                                       lines=[line()]))
        with self.assertRaises(ComplianceError) as cm:
            w.svc.settle_partial("inv-x", [
                {"artifact_id": "a-1", "fund_id": "fund-general", "amount": Decimal("10"),
                 "shares": [{"material_id": "m-hardener", "quantity": "1", "commitment_id": "po-1",
                             "material_origin": "imported",
                             "expense_category": "consolidation_material"}]}])
        self.assertEqual(cm.exception.code, "RECEIPT_NOT_ACCEPTED")


class TestMonthEndReport(unittest.TestCase):
    def test_identities_and_rejected_bases(self):
        w = World(emergency=True, deadline="2026-09-19", status="pending_approval")
        c = w.commitment(date="2026-09-10", qty="10", price="100.00")
        w.invoice(date="2026-09-12")
        w.usage(split=("5", "5"))
        w.svc.settle("inv-1", {"m-hardener": "fund-general"})
        w.svc.pay_invoice("inv-1", "2026-09-15")       # 待审拒付
        rpt = w.svc.month_end_report("2026-09")
        f = rpt["funds"]["fund-general"]
        # 预算 = 承诺余量 + 已核销 + 可用
        self.assertEqual(
            f["budget_base"],
            f["committed_open_base"] + f["expensed_base"] + f["available_base"])
        self.assertTrue(f["identity_ok"])
        # 器物实际消耗 = 账面分摊（1000 EUR × 7.90）
        for art in ("a-1", "a-2"):
            a = rpt["artifacts"][art]
            self.assertTrue(a["consumption_matches_ledger"])
            self.assertEqual(a["allocated_base"], q2(500 * 7.90))
        # 拒付逐笔带条款依据
        rej = rpt["rejected_payments"]
        self.assertTrue(rej and rej[0]["bases"])


if __name__ == "__main__":
    unittest.main()
