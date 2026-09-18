"""汇率表：跨币种金额按业务日锁定汇率。

约定：汇率表以人民币为本位币，rate[currency] 表示每 1 单位外币折合的人民币数。
跨币种凭证在其业务日取当日汇率，换算结果与所引汇率一并写入凭证，
此后即使汇率表补录或变动，已入账金额不重估。
"""

from datetime import date
from decimal import Decimal

from .errors import ComplianceError
from .money import D, q2


class FXBook:
    def __init__(self, rates_by_date: dict[str, dict[str, float | str]] | None = None,
                 base: str = "CNY"):
        self.base = base
        # {date: {currency: rate}}
        self._rates: dict[date, dict[str, Decimal]] = {}
        for d, table in (rates_by_date or {}).items():
            self.set_rate(d, table)

    def set_rate(self, business_date: str | date, table: dict[str, float | str]) -> None:
        if isinstance(business_date, str):
            business_date = date.fromisoformat(business_date)
        self._rates[business_date] = {
            cur: (Decimal("1") if cur == self.base else D(rate))
            for cur, rate in table.items()
        }
        self._rates[business_date].setdefault(self.base, Decimal("1"))

    def rate(self, currency: str, business_date: str | date) -> Decimal:
        if isinstance(business_date, str):
            business_date = date.fromisoformat(business_date)
        if currency == self.base:
            return Decimal("1")
        table = self._rates.get(business_date)
        if table is None or currency not in table:
            raise ComplianceError(
                "FX_RATE_MISSING",
                f"缺少 {business_date} 的 {currency} 汇率，无法按业务日锁定换算",
            )
        return table[currency]

    def to_base(self, amount, currency: str, business_date: str | date) -> Decimal:
        """按业务日汇率换算为本位币并舍入到分。"""
        return q2(D(amount) * self.rate(currency, business_date))
