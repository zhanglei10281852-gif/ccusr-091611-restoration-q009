"""不可变凭证账：结账后更正只能形成反向凭证。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


class PeriodClosedError(Exception):
    pass


class LedgerError(Exception):
    pass


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


@dataclass(frozen=True)
class Entry:
    account: str
    fund_id: str
    debit: Decimal
    credit: Decimal
    currency: str
    artifact_id: Optional[str] = None
    memo: str = ""


@dataclass
class Voucher:
    voucher_id: str
    entry_type: str          # budget|commitment|invoice|allocation|return|payment|reversal
    period: str
    business_date: date
    entries: list[Entry]
    reversal_of: Optional[str] = None
    memo: str = ""


class Ledger:
    """凭证只增不改；已结账期间拒绝直接入账。"""

    def __init__(self):
        self.vouchers: list[Voucher] = []
        self.closed_periods: set[str] = set()

    def post(self, voucher: Voucher) -> Voucher:
        if voucher.period in self.closed_periods:
            raise PeriodClosedError(
                f"期间 {voucher.period} 已结账，禁止直接入账；更正应形成反向凭证")
        for ccy in {e.currency for e in voucher.entries}:
            dr = sum(e.debit for e in voucher.entries if e.currency == ccy)
            cr = sum(e.credit for e in voucher.entries if e.currency == ccy)
            if dr != cr:
                raise LedgerError(f"凭证 {voucher.voucher_id} 币种 {ccy} 借贷不平")
        self.vouchers.append(voucher)
        return voucher

    def close_period(self, period: str) -> None:
        self.closed_periods.add(period)

    def get(self, voucher_id: str) -> Voucher:
        for v in self.vouchers:
            if v.voucher_id == voucher_id:
                return v
        raise LedgerError(f"凭证不存在：{voucher_id}")

    def reverse(self, voucher_id: str, new_voucher_id: str,
                reason: str, business_date: date) -> Voucher:
        """对原凭证生成反向凭证（借贷对调），原凭证保持不变。"""
        original = self.get(voucher_id)
        mirror = [
            Entry(account=e.account, fund_id=e.fund_id,
                  debit=e.credit, credit=e.debit,
                  currency=e.currency, artifact_id=e.artifact_id,
                  memo=f"冲回 {voucher_id}：{reason}")
            for e in original.entries
        ]
        voucher = Voucher(
            voucher_id=new_voucher_id,
            entry_type="reversal",
            period=period_of(business_date),
            business_date=business_date,
            entries=mirror,
            reversal_of=voucher_id,
            memo=reason,
        )
        return self.post(voucher)

    def account_balance(self, account: str, fund_id: str, currency: str,
                        as_of: Optional[date] = None) -> Decimal:
        total = Decimal("0")
        for v in self.vouchers:
            if as_of is not None and v.business_date > as_of:
                continue
            for e in v.entries:
                if e.account == account and e.fund_id == fund_id and e.currency == currency:
                    total += e.debit - e.credit
        return total
