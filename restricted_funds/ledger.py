"""凭证台账：只追加、期间结账、反向凭证。

所有凭证同时记录原币金额、业务日锁定汇率与本位币金额；
已入账凭证不可修改，结账后的更正只能以反向凭证冲回。
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .errors import ComplianceError
from .money import D


@dataclass
class Voucher:
    entry_id: str
    entry_type: str              # budget/commitment/invoice/allocation/return/payment/reversal
    business_date: str
    fund_id: str | None
    amount: Decimal              # 原币金额（带符号）
    currency: str
    fx_rate: Decimal | None      # 业务日锁定汇率（1 单位外币折本币）
    amount_base: Decimal         # 本位币金额（带符号）
    refs: dict = field(default_factory=dict)
    memo: str = ""
    posted_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    reversed_by: str | None = None


class Ledger:
    def __init__(self):
        self._entries: list[Voucher] = []
        self._closed_periods: set[str] = set()
        self._seq = 0

    @property
    def entries(self) -> list[Voucher]:
        return list(self._entries)

    def post(self, entry_type: str, business_date: str, fund_id: str | None,
             amount, currency: str, fx_rate, amount_base,
             refs: dict | None = None, memo: str = "") -> Voucher:
        period = business_date[:7]
        if period in self._closed_periods:
            raise ComplianceError(
                "PERIOD_CLOSED",
                f"会计期间 {period} 已结账，不能在该期间补记凭证；请在当前期间出具反向凭证",
            )
        self._seq += 1
        voucher = Voucher(
            entry_id=f"v-{self._seq:05d}",
            entry_type=entry_type,
            business_date=business_date,
            fund_id=fund_id,
            amount=D(amount),
            currency=currency,
            fx_rate=D(fx_rate) if fx_rate is not None else None,
            amount_base=D(amount_base),
            refs=refs or {},
            memo=memo,
        )
        self._entries.append(voucher)
        return voucher

    def close_period(self, period: str) -> None:
        self._closed_periods.add(period)

    def is_closed(self, period: str) -> bool:
        return period in self._closed_periods

    def get(self, entry_id: str) -> Voucher:
        for v in self._entries:
            if v.entry_id == entry_id:
                return v
        raise KeyError(f"凭证不存在：{entry_id}")

    def find(self, entry_type: str | None = None, fund_id: str | None = None,
             **refs) -> list[Voucher]:
        out = []
        for v in self._entries:
            if entry_type is not None and v.entry_type != entry_type:
                continue
            if fund_id is not None and v.fund_id != fund_id:
                continue
            if any(v.refs.get(k) != val for k, val in refs.items()):
                continue
            out.append(v)
        return out

    def live_sum(self, entry_type: str, fund_id: str | None = None, **refs) -> Decimal:
        """未被反向冲销的凭证本位币金额合计。"""
        total = Decimal("0")
        for v in self.find(entry_type, fund_id, **refs):
            if v.reversed_by is not None:
                continue
            total += v.amount_base
        return total
