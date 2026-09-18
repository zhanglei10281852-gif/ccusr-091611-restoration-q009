"""受限资金管理服务：业务规则全部在此汇合。

不变量：
- 金额一律 Decimal；跨币种凭证按“业务日”汇率换算并锁定，写入凭证后不重估；
- 预算新版本只可动用尚未承诺/尚未核销的额度；
- 紧急采购以 pending_approval 即时占用待审额度，批准期内可验收分摊，
  批准后方可付款；逾期未批自动转 expired 并冻结后续付款、释放占用；
- 同一发票并发核销累计不得超过含税总额；
- 退料按原分摊比例生成负向凭证冲回，不产生无来源余额；
- 已结账期间只能在当前期间出具反向凭证。
"""

import threading
from collections import defaultdict
from decimal import Decimal

from .errors import ComplianceError
from .fx import FXBook
from .ledger import Ledger
from .models import (
    Artifact,
    BudgetVersion,
    Commitment,
    Invoice,
    MaterialUsage,
    Receipt,
)
from .money import D, allocate_by_ratio, q2
from .terms import Fund

FREEZE_CLAUSE = "《紧急采购管理办法》第6条：紧急采购须在批准期限内完成追认审批，逾期未批的，冻结后续一切付款"


class RestrictedFundsService:
    def __init__(self, fx: FXBook):
        self.fx = fx
        self.ledger = Ledger()
        self.funds: dict[str, Fund] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.budget_versions: dict[str, list[BudgetVersion]] = defaultdict(list)
        self.commitments: dict[str, Commitment] = {}
        self.invoices: dict[str, Invoice] = {}
        self.receipts: dict[str, Receipt] = {}
        self.usages: list[MaterialUsage] = []
        self.returned_qty: dict[tuple, Decimal] = defaultdict(lambda: Decimal("0"))
        self.rejected_payments: list[dict] = []
        self._lock = threading.RLock()

    # ---------- 主数据 ----------

    def register_fund(self, fund: Fund) -> None:
        self.funds[fund.fund_id] = fund

    def register_artifact(self, artifact: Artifact) -> None:
        self.artifacts[artifact.artifact_id] = artifact

    # ---------- 预算版本 ----------

    def add_budget_version(self, version: BudgetVersion) -> Decimal:
        fund = self._fund(version.fund_id)
        new_base = self.fx.to_base(version.amount, fund.currency, version.effective_date)
        occupied = self.occupied_base(version.fund_id)
        if new_base < occupied - Decimal("0.01"):
            blockers = [
                c.commitment_id for c in self.commitments.values()
                if c.fund_id == version.fund_id and c.is_open
            ]
            raise ComplianceError(
                "COMMITMENT_ADJUSTMENT_BLOCKED",
                f"预算调整为 {q2(new_base)} {self.fx.base}，低于已承诺及已核销占用 "
                f"{q2(occupied)}；阻塞承诺：{', '.join(blockers) or '（已核销支出）'}（调整只能影响尚未承诺的额度）",
                bases=["《预算管理办法》第4条：预算调减不得冲减已形成的承诺与支出"],
            )
        voucher = self.ledger.post(
            "budget", version.effective_date, fund.fund_id,
            version.amount, fund.currency,
            self.fx.rate(fund.currency, version.effective_date), new_base,
            refs={"version_id": version.version_id, "superseded": bool(self.budget_versions[fund.fund_id])},
            memo=f"预算版本 {version.version_id}{'（取代旧版本）' if self.budget_versions[fund.fund_id] else ''}：{version.note}",
        )
        self.budget_versions[fund.fund_id].append(version)
        return voucher.amount_base

    def budget_total_base(self, fund_id: str) -> Decimal:
        versions = self.budget_versions.get(fund_id)
        if not versions:
            return Decimal("0.00")
        v = versions[-1]
        return self.fx.to_base(v.amount, self._fund(fund_id).currency, v.effective_date)

    # ---------- 采购承诺 ----------

    def create_commitment(self, commitment: Commitment) -> Decimal:
        fund = self._fund(commitment.fund_id)
        # 条款预检：承诺内任一明细违反基金限制即不得占用该基金
        violations = self._lines_violations(commitment)
        if violations:
            raise ComplianceError(
                "TERM_DENIED",
                f"承诺 {commitment.commitment_id} 含受限采购明细，不得由基金 {fund.fund_id} 支付",
                bases=sorted({b for bs in violations.values() for b in bs}),
            )
        # 承诺币种可以与预算币种不同（跨币种采购），统一折本币核额
        amount_base = self.fx.to_base(commitment.amount, commitment.currency, commitment.business_date)
        projected = self.occupied_base(fund.fund_id) + amount_base
        if projected > self.budget_total_base(fund.fund_id) + Decimal("0.01"):
            raise ComplianceError(
                "BUDGET_EXCEEDED",
                f"承诺 {commitment.amount} {commitment.currency}（折 {q2(amount_base)}）"
                f"超出基金可用余额（占用后 {q2(projected)} > 预算 {q2(self.budget_total_base(fund.fund_id))}）",
            )
        voucher = self.ledger.post(
            "commitment", commitment.business_date, fund.fund_id,
            commitment.amount, commitment.currency,
            self.fx.rate(commitment.currency, commitment.business_date), amount_base,
            refs={"commitment_id": commitment.commitment_id,
                  "emergency": commitment.emergency,
                  "status_at_post": commitment.status},
            memo=("紧急采购待审追认" if commitment.emergency else "采购承诺") + f" {commitment.commitment_id}",
        )
        commitment.matched_base = Decimal("0.00")
        commitment.matched_ccy = Decimal("0.00")
        commitment.base_amount = voucher.amount_base
        commitment.fx_rate = voucher.fx_rate
        self.commitments[commitment.commitment_id] = commitment
        return amount_base

    def approve_commitment(self, commitment_id: str, approval_date: str) -> None:
        with self._lock:
            c = self.commitments[commitment_id]
            # 先判定本承诺是否逾期（不能被全局过期扫描抢先改成 expired）
            if c.status == "pending_approval" and c.approval_deadline \
                    and approval_date > c.approval_deadline:
                self._freeze(c, approval_date)
                raise ComplianceError(
                    "EMERGENCY_OVERDUE_FREEZE",
                    f"紧急承诺 {commitment_id} 已于 {c.approval_deadline} 到期，{approval_date} 才报批，冻结",
                    bases=[FREEZE_CLAUSE],
                )
            self._expire_overdue(approval_date)
            if c.status != "pending_approval":
                raise ComplianceError("COMMITMENT_NOT_APPROVED",
                                      f"承诺 {commitment_id} 状态为 {c.status}，不能批准")
            c.status = "approved"
            c.approved_at = approval_date

    def _expire_overdue(self, as_of: str) -> list[str]:
        expired = []
        for c in self.commitments.values():
            if c.status == "pending_approval" and c.approval_deadline and as_of > c.approval_deadline:
                self._freeze(c, as_of)
                expired.append(c.commitment_id)
        return expired

    def _freeze(self, c: Commitment, as_of: str) -> None:
        c.status = "expired"
        # 冻结凭证登记在发现日（当前期间），但金额与汇率沿用承诺日锁定值，
        # 与原待审占用凭证恰好抵消，不产生汇兑差
        self.ledger.post(
            "commitment", as_of, c.fund_id,
            -c.amount, c.currency, c.fx_rate, -c.base_amount,
            refs={"commitment_id": c.commitment_id, "kind": "overdue_release", "frozen": True},
            memo=f"紧急承诺 {c.commitment_id} 逾期未批，冻结后续付款并释放待审占用额度",
        )

    def occupied_base(self, fund_id: str) -> Decimal:
        """预算占用（本币）= 承诺余量（按承诺日锁定汇率估值）+ 已核销净额。

        已核销部分按各自发票锁定汇率入账；承诺余量始终按承诺日汇率估值，
        汇率波动既不凭空增加占用，也不掩盖超支。
        """
        total = Decimal("0")
        for c in self.commitments.values():
            if c.fund_id != fund_id or not c.is_open:
                continue
            total += self._residual_base(c)
        total += self.expensed_base(fund_id)
        return q2(total)

    def _convert_ccy(self, amount, from_ccy: str, to_ccy: str, business_date: str) -> Decimal:
        """经本位币套算的跨币种换算（按指定业务日汇率）。"""
        if from_ccy == to_ccy:
            return D(amount)
        base = self.fx.to_base(amount, from_ccy, business_date)
        return q2(base / self.fx.rate(to_ccy, business_date))

    # ---------- 发票 / 验收 / 用量 ----------

    def register_invoice(self, invoice: Invoice) -> None:
        if invoice.lines:
            net = invoice.net_total
            gross = q2(net * (Decimal("1") + D(invoice.tax_rate)))
            if gross != q2(invoice.gross_amount):
                raise ValueError(
                    f"发票 {invoice.invoice_id} 明细净额 {net} ×(1+{invoice.tax_rate})={gross} "
                    f"与含税总额 {invoice.gross_amount} 不符"
                )
        self.invoices[invoice.invoice_id] = invoice
        self.ledger.post(
            "invoice", invoice.business_date, None,
            invoice.gross_amount, invoice.currency,
            self.fx.rate(invoice.currency, invoice.business_date),
            self.fx.to_base(invoice.gross_amount, invoice.currency, invoice.business_date),
            refs={"invoice_id": invoice.invoice_id, "vendor": invoice.vendor},
            memo=f"发票 {invoice.invoice_id}（{invoice.vendor}）",
        )

    def register_receipt(self, receipt: Receipt) -> None:
        inv = self.invoices[receipt.invoice_id]
        if inv.lines:
            for line in inv.lines:
                got = D(receipt.accepted_quantities.get(line.material_id, 0))
                if receipt.accepted and got < line.quantity:
                    raise ComplianceError(
                        "RECEIPT_NOT_ACCEPTED",
                        f"验收数量不足：{line.material_id} 应收 {line.quantity} 实收 {got}",
                    )
        self.receipts[receipt.invoice_id] = receipt

    def register_usage(self, usage: MaterialUsage) -> None:
        self.usages.append(usage)

    # ---------- 核销分摊 ----------

    def settle(self, invoice_id: str, funding_plan: dict[str, str],
               business_date: str | None = None) -> list[str]:
        """按材料实际用量把整张发票含税总额分摊到器物 × 基金。

        funding_plan: {material_id: fund_id} —— 每种材料由哪个基金支付，
        服务逐条核对资助条款，违规整笔拒绝并给出条款依据。
        business_date 缺省为发票业务日（汇率随之锁定）。
        """
        inv = self._invoice(invoice_id)
        post_date = business_date or inv.business_date
        if not inv.lines:
            raise ComplianceError("RECEIPT_NOT_ACCEPTED",
                                  f"发票 {invoice_id} 缺少材料明细，无法按用量自动分摊，请改用 settle_partial")
        receipt = self.receipts.get(invoice_id)
        if receipt is None or not receipt.accepted:
            raise ComplianceError("RECEIPT_NOT_ACCEPTED",
                                  f"发票 {invoice_id} 未通过到货验收，不得核销")
        usages = [u for u in self.usages if u.invoice_id == invoice_id]
        pieces = []          # 每个“器物 × 材料”一个最小分摊片
        used_qty: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for u in usages:
            line = self._line_of_material(inv, u.material_id)
            used_qty[u.material_id] += D(u.quantity)
            fund_id = funding_plan.get(u.material_id)
            if not fund_id:
                raise ComplianceError("TERM_DENIED",
                                      f"材料 {u.material_id} 未指定支付基金（funding_plan 缺项）")
            artifact = self.artifacts[u.artifact_id]
            violations = self.funds[fund_id].evaluate({
                "material_origin": line.material_origin,
                "expense_category": line.expense_category,
                "artifact_class": artifact.artifact_class,
                "business_date": post_date,
            })
            if violations:
                raise ComplianceError(
                    "TERM_DENIED",
                    f"器物 {u.artifact_id} 使用的 {line.material_name}（{line.material_origin}）"
                    f"不得由基金 {fund_id} 支付",
                    bases=violations,
                )
            commitment = self.commitments.get(line.commitment_id) if line.commitment_id else None
            if commitment and commitment.fund_id != fund_id:
                raise ComplianceError(
                    "TERM_DENIED",
                    f"材料 {u.material_id} 的承诺 {commitment.commitment_id} 属于基金 "
                    f"{commitment.fund_id}，与资金计划 {fund_id} 不一致",
                )
            pieces.append({
                "artifact_id": u.artifact_id,
                "fund_id": fund_id,
                "net": q2(D(u.quantity) * line.unit_price),
                "share": {
                    "material_id": line.material_id,
                    "quantity": str(u.quantity),
                    "commitment_id": line.commitment_id,
                    "material_origin": line.material_origin,
                    "expense_category": line.expense_category,
                },
            })
        if not pieces:
            raise ComplianceError("RECEIPT_NOT_ACCEPTED", f"发票 {invoice_id} 没有任何用量记录")
        for material_id, qty in used_qty.items():
            accepted = D(receipt.accepted_quantities.get(material_id, 0))
            if qty > accepted + Decimal("0.0001"):
                raise ComplianceError(
                    "RECEIPT_NOT_ACCEPTED",
                    f"材料 {material_id} 实际领用 {qty} 超过验收合格数量 {accepted}",
                )
        # 含税总额按各片净额比例一次切分，尾差并入最大片，保证合计恰为含税总额
        amounts = allocate_by_ratio(D(inv.gross_amount), [p["net"] for p in pieces])
        items = []
        for piece, amount in zip(pieces, amounts):
            items.append({
                "artifact_id": piece["artifact_id"],
                "fund_id": piece["fund_id"],
                "amount": amount,
                "shares": [piece["share"]],
            })
        return self._post_allocations(inv, items)

    def settle_partial(self, invoice_id: str, items: list[dict],
                       business_date: str | None = None) -> list[str]:
        """显式金额核销（供并发/部分到账/跨期补摊场景）；累计超过含税总额即拒。

        item: {artifact_id, fund_id, amount, shares:[{material_id, quantity,
              commitment_id, material_origin, expense_category}]}
        business_date 缺省为发票业务日（汇率随之锁定）。
        """
        inv = self._invoice(invoice_id)
        receipt = self.receipts.get(invoice_id)
        if receipt is None or not receipt.accepted:
            raise ComplianceError("RECEIPT_NOT_ACCEPTED",
                                  f"发票 {invoice_id} 未通过到货验收，不得核销")
        for item in items:
            fund = self._fund(item["fund_id"])
            artifact = self.artifacts[item["artifact_id"]]
            for share in item["shares"]:
                violations = fund.evaluate({
                    "material_origin": share["material_origin"],
                    "expense_category": share["expense_category"],
                    "artifact_class": artifact.artifact_class,
                    "business_date": business_date or inv.business_date,
                })
                if violations:
                    raise ComplianceError("TERM_DENIED",
                                          f"核销片段违反基金 {fund.fund_id} 限制", bases=violations)
                cid = share.get("commitment_id")
                if cid and self.commitments[cid].fund_id != fund.fund_id:
                    raise ComplianceError(
                        "TERM_DENIED",
                        f"承诺 {cid} 属于基金 {self.commitments[cid].fund_id}，"
                        f"不得核销到基金 {fund.fund_id}",
                    )
        return self._post_allocations(inv, items, business_date)

    def _post_allocations(self, inv: Invoice, items: list[dict],
                          post_date: str | None = None) -> list[str]:
        post_date = post_date or inv.business_date
        # 汇率按入账业务日锁定（通常即发票业务日），与付款日无关
        rate = self.fx.rate(inv.currency, post_date)
        total = sum((q2(D(i["amount"])) for i in items), Decimal("0"))
        # 同组合并（同器物同基金一片），同时把材料明细并到 shares 供退料与条款复核
        merged: dict[tuple, dict] = {}
        for item in items:
            key = (item["artifact_id"], item["fund_id"])
            if key not in merged:
                merged[key] = {"artifact_id": key[0], "fund_id": key[1],
                               "amount": Decimal("0"), "shares": []}
            merged[key]["amount"] += D(item["amount"])
            merged[key]["shares"].extend(item["shares"])
        add_by_fund: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        consume_by_commitment: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for m in merged.values():
            amount = q2(m["amount"])
            add_by_fund[m["fund_id"]] += q2(amount * rate)
            for share in m["shares"]:
                cid = share.get("commitment_id")
                if cid and self.commitments[cid].is_open:
                    consume_by_commitment[cid] += q2(amount * rate)
        ids = []
        with self._lock:
            already = self.allocated_ccy(inv.invoice_id)
            if q2(already + total) > q2(inv.gross_amount) + Decimal("0.01"):
                raise ComplianceError(
                    "INVOICE_GROSS_EXCEEDED",
                    f"发票 {inv.invoice_id} 含税总额 {inv.gross_amount} {inv.currency}，"
                    f"已核销 {q2(already)}，本次 {q2(total)} 将超额（并发核销受含税总额约束）",
                )
            # 预算足额预检（先校验后入账）：新占用 = 新增核销 − 将被消化的承诺余量
            for fund_id, add_base in add_by_fund.items():
                consumed_residual = Decimal("0")
                for cid, consume in consume_by_commitment.items():
                    c = self.commitments[cid]
                    if c.fund_id != fund_id or not c.is_open:
                        continue
                    residual = self._residual_base(c)
                    consumed_residual += min(consume, residual)
                projected = self.occupied_base(fund_id) + add_base - consumed_residual
                if projected > self.budget_total_base(fund_id) + Decimal("0.01"):
                    raise ComplianceError(
                        "BUDGET_EXCEEDED",
                        f"基金 {fund_id} 核销后占用 {q2(projected)} 超过预算 "
                        f"{q2(self.budget_total_base(fund_id))}（超承诺部分无预算来源）",
                    )
            for m in merged.values():
                amount = q2(m["amount"])
                amount_base = q2(amount * rate)
                voucher = self.ledger.post(
                    "allocation", post_date, m["fund_id"],
                    amount, inv.currency, rate, amount_base,
                    refs={"invoice_id": inv.invoice_id,
                          "artifact_id": m["artifact_id"],
                          "shares": m["shares"]},
                    memo=f"发票 {inv.invoice_id} 分摊至器物 {m['artifact_id']}",
                )
                ids.append(voucher.entry_id)
                # 回流承诺占用：已核销部分从“已承诺未核销”转入“已核销”
                touched: set[str] = set()
                for share in m["shares"]:
                    cid = share.get("commitment_id")
                    if cid and self.commitments[cid].is_open:
                        c = self.commitments[cid]
                        touched.add(cid)
                        if len(m["shares"]) == 1:
                            part_ccy = amount
                        else:
                            net_weight = (D(share["quantity"]) *
                                          D(self._line_of_material(inv, share["material_id"]).unit_price))
                            net_total = sum(
                                D(s["quantity"]) *
                                D(self._line_of_material(inv, s["material_id"]).unit_price)
                                for s in m["shares"])
                            part_ccy = q2(amount * net_weight / net_total)
                        # 发票币种与承诺币种一致时直接冲减；跨币种按发票业务日套算
                        part_in_commit_ccy = part_ccy if inv.currency == c.currency else \
                            self._convert_ccy(part_ccy, inv.currency, c.currency, post_date)
                        c.matched_ccy = q2(c.matched_ccy + part_in_commit_ccy)
                        c.matched_base = q2(c.matched_base + q2(part_ccy * rate))
                        c.delivered_qty[share["material_id"]] = (
                            D(c.delivered_qty.get(share["material_id"], 0)) + D(share["quantity"]))
                for cid in touched:
                    c = self.commitments[cid]
                    if c.status == "approved":
                        ordered = {l.material_id: D(l.quantity) for l in c.lines}
                        if all(D(c.delivered_qty.get(mid, 0)) >= q for mid, q in ordered.items()):
                            c.status = "completed"
        return ids

    @staticmethod
    def _residual_base(c: Commitment) -> Decimal:
        """承诺余量：未交付数量×承诺净单价，再按承诺日锁定汇率折本币。"""
        residual_ccy = Decimal("0")
        for line in c.lines:
            undelivered = D(line.quantity) - D(c.delivered_qty.get(line.material_id, 0))
            if undelivered > 0:
                residual_ccy += undelivered * D(line.unit_price)
        return q2(residual_ccy * c.fx_rate)

    def _reopen_if_incomplete(self, c: Commitment) -> None:
        if c.status == "completed" and self._residual_base(c) > 0:
            c.status = "approved"

    @staticmethod
    def _is_expense_entry(v) -> bool:
        """影响费用/核销占用的凭证：分摊、退料、以及冲销分摊的反向凭证。"""
        if v.entry_type in ("allocation", "return"):
            return True
        return v.entry_type == "reversal" and v.refs.get("original_type", "allocation") == "allocation"

    @staticmethod
    def _is_payment_entry(v) -> bool:
        if v.entry_type == "payment":
            return True
        return v.entry_type == "reversal" and v.refs.get("original_type") == "payment"

    def allocated_ccy(self, invoice_id: str) -> Decimal:
        """发票下净核销原币金额（allocation/return/分摊冲销全部带符号）。"""
        total = Decimal("0")
        for v in self.ledger.entries:
            if self._is_expense_entry(v) and v.refs.get("invoice_id") == invoice_id:
                total += v.amount
        return q2(total)

    def expensed_base(self, fund_id: str, upto_period: str | None = None) -> Decimal:
        total = Decimal("0")
        for v in self.ledger.entries:
            if self._is_expense_entry(v) and v.fund_id == fund_id:
                if upto_period is None or v.business_date[:7] <= upto_period:
                    total += v.amount_base
        return q2(total)

    def paid_base(self, fund_id: str, upto_period: str | None = None) -> Decimal:
        total = Decimal("0")
        for v in self.ledger.entries:
            if self._is_payment_entry(v) and v.fund_id == fund_id:
                if upto_period is None or v.business_date[:7] <= upto_period:
                    total += v.amount_base
        return q2(total)

    # ---------- 付款 ----------

    def pay_invoice(self, invoice_id: str, business_date: str) -> dict:
        """逐基金份额付款；合规份额支付，违规章份额记录拒绝原因与条款依据。"""
        inv = self._invoice(invoice_id)
        self._expire_overdue(business_date)
        receipt = self.receipts.get(invoice_id)
        result = {"paid": [], "rejected": []}
        fund_ids = sorted({
            v.fund_id for v in self.ledger.entries
            if self._is_expense_entry(v)
            and v.refs.get("invoice_id") == invoice_id and v.fund_id
        })
        for fund_id in fund_ids:
            amount_vouchers = [
                v for v in self.ledger.entries
                if self._is_expense_entry(v)
                and v.refs.get("invoice_id") == invoice_id and v.fund_id == fund_id
            ]
            net_base = q2(sum((v.amount_base for v in amount_vouchers), Decimal("0")))
            net_ccy = q2(sum((v.amount for v in amount_vouchers), Decimal("0")))
            if net_ccy == 0:
                continue
            paid_ccy = q2(sum((
                v.amount for v in self.ledger.entries
                if self._is_payment_entry(v)
                and v.fund_id == fund_id and v.refs.get("invoice_id") == invoice_id
            ), Decimal("0")))
            if paid_ccy >= net_ccy - Decimal("0.01"):
                continue                       # 该基金份额已付清，付款幂等
            pay_ccy = q2(net_ccy - paid_ccy)
            pay_base = q2(net_base * pay_ccy / net_ccy)
            # 1) 验收
            if receipt is None or not receipt.accepted:
                result["rejected"].append(self._reject(
                    business_date, invoice_id, fund_id, pay_base,
                    "RECEIPT_NOT_ACCEPTED", f"发票 {invoice_id} 未通过到货验收", []))
                continue
            # 2) 条款（仅对仍生效的分摊凭证，用核销时快照的材料上下文复核）
            bases, frozen, unapproved = [], False, False
            for v in amount_vouchers:
                if v.entry_type != "allocation" or v.reversed_by is not None:
                    continue
                artifact = self.artifacts[v.refs["artifact_id"]]
                for share in v.refs.get("shares", []):
                    bases += self.funds[fund_id].evaluate({
                        "material_origin": share.get("material_origin"),
                        "expense_category": share.get("expense_category"),
                        "artifact_class": artifact.artifact_class,
                        "business_date": inv.business_date,
                    })
                    cid = share.get("commitment_id")
                    if cid:
                        c = self.commitments[cid]
                        if c.status == "expired":
                            frozen = True
                        elif c.status == "pending_approval":
                            unapproved = True
            if bases:
                result["rejected"].append(self._reject(
                    business_date, invoice_id, fund_id, pay_base,
                    "TERM_DENIED", f"付款复核发现受限材料由 {fund_id} 支付", sorted(set(bases))))
                continue
            # 3) 紧急采购状态
            if frozen:
                result["rejected"].append(self._reject(
                    business_date, invoice_id, fund_id, pay_base,
                    "EMERGENCY_OVERDUE_FREEZE",
                    f"发票 {invoice_id} 关联紧急采购逾期未获追认，{fund_id} 冻结后续付款",
                    [FREEZE_CLAUSE]))
                continue
            if unapproved:
                result["rejected"].append(self._reject(
                    business_date, invoice_id, fund_id, pay_base,
                    "COMMITMENT_NOT_APPROVED",
                    f"发票 {invoice_id} 关联紧急采购尚在待审追认期内，暂不得付款",
                    ["《紧急采购管理办法》第5条：待审追认通过前不得办理付款"]))
                continue
            # 4) 预算余额不变式（付款不改变占用，双保险）
            if self.occupied_base(fund_id) > self.budget_total_base(fund_id) + Decimal("0.01"):
                result["rejected"].append(self._reject(
                    business_date, invoice_id, fund_id, pay_base,
                    "BUDGET_EXCEEDED", f"基金 {fund_id} 预算余额不足", []))
                continue
            # 锁定汇率沿用发票业务日（付款凭证记录所引汇率，不按付款日重估）
            rate = self.fx.rate(inv.currency, inv.business_date)
            voucher = self.ledger.post(
                "payment", business_date, fund_id,
                pay_ccy, inv.currency, rate, pay_base,
                refs={"invoice_id": invoice_id, "fx_locked_from": inv.business_date},
                memo=f"支付发票 {invoice_id} 的 {fund_id} 份额（锁定 {inv.business_date} 汇率）",
            )
            result["paid"].append({"entry_id": voucher.entry_id, "fund_id": fund_id,
                                   "amount": pay_ccy, "currency": inv.currency, "amount_base": pay_base})
        return result

    def _reject(self, date_, invoice_id, fund_id, amount_base, code, message, bases):
        rec = {"business_date": date_, "invoice_id": invoice_id, "fund_id": fund_id,
               "amount_base": amount_base, "code": code, "message": message, "bases": bases}
        self.rejected_payments.append(rec)
        return rec

    # ---------- 退料：沿原分摊比例冲回 ----------

    def return_material(self, invoice_id: str, material_id: str, quantity,
                        business_date: str) -> list[str]:
        inv = self._invoice(invoice_id)
        qty = D(quantity)
        # 退料冲回沿用发票业务日锁定汇率，与退料申请日无关，不产生汇兑差/无来源余额
        rate = self.fx.rate(inv.currency, inv.business_date)
        ids = []
        with self._lock:
            source_vouchers = []
            for v in self.ledger.find("allocation", None, invoice_id=invoice_id):
                if v.reversed_by is not None:
                    continue
                for share in v.refs.get("shares", []):
                    if share["material_id"] == material_id:
                        source_vouchers.append((v, share))
            if not source_vouchers:
                raise ComplianceError("RECEIPT_NOT_ACCEPTED",
                                      f"发票 {invoice_id} 下找不到材料 {material_id} 的原分摊")
            original_qty = sum((D(s["quantity"]) for _, s in source_vouchers), Decimal("0"))
            returned = self.returned_qty[(invoice_id, material_id)]
            if returned + qty > original_qty + Decimal("0.0001"):
                raise ComplianceError(
                    "RECEIPT_NOT_ACCEPTED",
                    f"退料 {qty} 超过可退数量（原领用 {original_qty}，已退 {returned}）",
                )
            # 沿原分摊比例：每片退额 = 原片金额 × 退料量/原领量；尾差并入最大片
            ratios = [D(s["quantity"]) for _, s in source_vouchers]
            base = sum(ratios, Decimal("0"))
            parts = allocate_by_ratio(
                q2(self._gross_unit(inv, material_id) * qty), ratios)
            for (v, share), part, ratio in zip(source_vouchers, parts, ratios):
                if part == 0:
                    continue
                part_base = q2(part * rate)
                piece_qty = qty * ratio / base      # 该片器物按原比例承担的退料量
                rv = self.ledger.post(
                    "return", business_date, v.fund_id,
                    -part, inv.currency, rate, -part_base,
                    refs={"invoice_id": invoice_id, "artifact_id": v.refs["artifact_id"],
                          "original_allocation": v.entry_id, "material_id": material_id,
                          "quantity": str(piece_qty)},
                    memo=f"退料 {material_id} {qty} 沿原分摊冲回器物 {v.refs['artifact_id']}",
                )
                ids.append(rv.entry_id)
                cid = share.get("commitment_id")
                if cid:
                    c = self.commitments[cid]
                    part_in_ccy = part if inv.currency == c.currency else \
                        self._convert_ccy(part, inv.currency, c.currency, inv.business_date)
                    c.matched_ccy = q2(c.matched_ccy - part_in_ccy)
                    c.matched_base = q2(c.matched_base - part_base)
                    c.delivered_qty[material_id] = D(
                        c.delivered_qty.get(material_id, 0)) - piece_qty
                    self._reopen_if_incomplete(c)
            self.returned_qty[(invoice_id, material_id)] = q2(returned + qty)
        return ids

    @staticmethod
    def _gross_unit(inv: Invoice, material_id: str) -> Decimal:
        line = next((l for l in inv.lines if l.material_id == material_id), None)
        return q2(line.unit_price * (Decimal("1") + D(inv.tax_rate))) if line else Decimal("0")

    # ---------- 结账后更正：反向凭证 ----------

    def reverse_voucher(self, entry_id: str, business_date: str, reason: str) -> str:
        original = self.ledger.get(entry_id)
        # 反向凭证在当前（未结账）期间登记，但金额与折算沿用原凭证锁定汇率，
        # 本币冲回额与原凭证一分不差，不产生汇兑差或无来源余额
        rate = original.fx_rate if original.fx_rate is not None else Decimal("1")
        if original.entry_type == "allocation":
            paid = [
                p for p in self.ledger.find("payment", original.fund_id,
                                            invoice_id=original.refs.get("invoice_id"))
                if p.reversed_by is None
            ]
            if paid:
                raise ComplianceError(
                    "REVERSAL_BLOCKED",
                    f"分摊 {entry_id} 对应份额已付款，请先在本期反向付款凭证，再冲销分摊",
                )
        rv = self.ledger.post(
            "reversal", business_date, original.fund_id,
            -original.amount, original.currency, rate, -original.amount_base,
            refs={"reverses": entry_id,
                  "original_type": original.entry_type,
                  "invoice_id": original.refs.get("invoice_id"),
                  "artifact_id": original.refs.get("artifact_id"),
                  "fx_locked_from": original.business_date, "reason": reason},
            memo=f"反向冲销 {entry_id}：{reason}",
        )
        original.reversed_by = rv.entry_id
        if original.entry_type == "allocation":
            inv = self.invoices.get(original.refs.get("invoice_id"))
            for share in original.refs.get("shares", []):
                cid = share.get("commitment_id")
                if cid:
                    c = self.commitments[cid]
                    part = original.amount if inv is None or inv.currency == c.currency else \
                        self._convert_ccy(original.amount, inv.currency, c.currency, original.business_date)
                    c.matched_ccy = q2(c.matched_ccy - part)
                    c.matched_base = q2(c.matched_base - original.amount_base)
                    c.delivered_qty[share["material_id"]] = D(
                        c.delivered_qty.get(share["material_id"], 0)) - D(share["quantity"])
                    self._reopen_if_incomplete(c)
        return rv.entry_id

    def close_period(self, period: str) -> None:
        self.ledger.close_period(period)

    # ---------- 月底对账 ----------

    def month_end_report(self, period: str) -> dict:
        funds_report = {}
        for fund_id, fund in self.funds.items():
            versions = self.budget_versions.get(fund_id, [])
            v = versions[-1] if versions else None
            expensed = self.expensed_base(fund_id, period)
            committed_open = Decimal("0")
            pending = Decimal("0")
            for c in self.commitments.values():
                if c.fund_id != fund_id or not c.is_open:
                    continue
                residual = self._residual_base(c)
                committed_open += residual
                if c.status == "pending_approval":
                    pending += residual
            budget_base = self.budget_total_base(fund_id)
            paid = self.paid_base(fund_id, period)
            frozen = [c.commitment_id for c in self.commitments.values()
                      if c.fund_id == fund_id and c.status == "expired"]
            funds_report[fund_id] = {
                "name": fund.name,
                "currency": fund.currency,
                "budget_version": v.version_id if v else None,
                "budget_base": budget_base,
                "committed_open_base": q2(committed_open),
                "pending_approval_base": q2(pending),
                "expensed_base": expensed,
                "paid_base": paid,
                "available_base": q2(budget_base - committed_open - expensed),
                "frozen_commitments": frozen,
                # 对账恒等式：预算 = 已承诺未核销 + 已核销净额 + 可用余额
                "identity_ok": q2(committed_open + expensed +
                                  q2(budget_base - committed_open - expensed)) == budget_base,
            }
        artifacts_report = {}
        # 退料量按“发票 × 器物 × 材料”从退料凭证归集（沿原分摊冲回，不产生无来源余额）
        returned_by_art: dict[tuple, Decimal] = defaultdict(lambda: Decimal("0"))
        for x in self.ledger.entries:
            if x.entry_type == "return":
                key = (x.refs["invoice_id"], x.refs["artifact_id"], x.refs["material_id"])
                returned_by_art[key] += D(x.refs["quantity"])
        for art_id, art in self.artifacts.items():
            by_fund: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            for x in self.ledger.entries:
                if self._is_expense_entry(x) \
                        and x.refs.get("artifact_id") == art_id and x.business_date[:7] <= period:
                    by_fund[x.fund_id] += x.amount_base
            # 净用量（领用量 − 退料量）与按发票锁定汇率折算的含税金额
            net_qty: dict[tuple, Decimal] = defaultdict(lambda: Decimal("0"))
            for u in self.usages:
                if u.artifact_id == art_id and u.business_date[:7] <= period:
                    net_qty[(u.invoice_id, u.material_id)] += D(u.quantity)
            for (inv_id, aid, mid), qty in returned_by_art.items():
                if aid == art_id:
                    net_qty[(inv_id, mid)] -= qty
            consumed: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            consumed_value = Decimal("0")
            for (inv_id, mid), qty in net_qty.items():
                consumed[mid] += qty
                inv = self.invoices[inv_id]
                gross_ccy = qty * self._gross_unit(inv, mid)
                consumed_value += self.fx.to_base(gross_ccy, inv.currency, inv.business_date)
            allocated = q2(sum(by_fund.values(), Decimal("0")))
            artifacts_report[art_id] = {
                "name": art.name,
                "artifact_class": art.artifact_class,
                "consumed_quantity": {k: q2(v) for k, v in consumed.items()},
                "consumed_value_base": q2(consumed_value),
                "allocated_base": allocated,
                "by_fund": {k: q2(v) for k, v in by_fund.items()},
                "consumption_matches_ledger": allocated == q2(consumed_value),
            }
        invoices_report = []
        for inv_id, inv in self.invoices.items():
            if inv.business_date[:7] != period:
                continue
            allocated = self.allocated_ccy(inv_id)
            paid_ccy = q2(sum((
                x.amount for x in self.ledger.entries
                if self._is_payment_entry(x) and x.refs.get("invoice_id") == inv_id
            ), Decimal("0")))
            invoices_report.append({
                "invoice_id": inv_id,
                "currency": inv.currency,
                "gross_amount": q2(inv.gross_amount),
                "allocated_net": allocated,
                "unallocated": q2(inv.gross_amount - allocated),
                "paid_net": paid_ccy,
                "fully_settled": allocated == q2(inv.gross_amount),
            })
        return {
            "period": period,
            "funds": funds_report,
            "artifacts": artifacts_report,
            "invoices": invoices_report,
            "rejected_payments": list(self.rejected_payments),
        }

    # ---------- 内部工具 ----------

    def _fund(self, fund_id: str) -> Fund:
        if fund_id not in self.funds:
            raise KeyError(f"未登记基金：{fund_id}")
        return self.funds[fund_id]

    def _invoice(self, invoice_id: str) -> Invoice:
        if invoice_id not in self.invoices:
            raise KeyError(f"未登记发票：{invoice_id}")
        return self.invoices[invoice_id]

    def _line_of_material(self, inv: Invoice, material_id: str):
        line = next((l for l in inv.lines if l.material_id == material_id), None)
        if line is None:
            raise ComplianceError("RECEIPT_NOT_ACCEPTED",
                                  f"发票 {inv.invoice_id} 明细中没有材料 {material_id}")
        return line

    def _lines_violations(self, c: Commitment) -> dict[str, list[str]]:
        out = {}
        for line in c.lines:
            for art_id in c.artifact_ids:
                artifact = self.artifacts[art_id]
                bases = self.funds[c.fund_id].evaluate({
                    "material_origin": line.material_origin,
                    "expense_category": line.expense_category,
                    "artifact_class": artifact.artifact_class,
                    "business_date": c.business_date,
                })
                if bases:
                    out[f"{line.material_id}/{art_id}"] = bases
        return out
