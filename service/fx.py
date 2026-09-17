"""汇率表：跨币种金额按业务日锁定汇率。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal


class FxError(Exception):
    pass


class FxTable:
    def __init__(self):
        self._rates: dict[tuple[date, str, str], Decimal] = {}

    @classmethod
    def from_dict(cls, data: dict) -> "FxTable":
        table = cls()
        for row in data.get("rates", []):
            table.add_rate(date.fromisoformat(row["date"]), row["base"], row["quote"], row["rate"])
        return table

    def add_rate(self, day: date, base: str, quote: str, rate) -> None:
        self._rates[(day, base, quote)] = Decimal(str(rate))

    def rate_on(self, day: date, base: str, quote: str) -> Decimal:
        """业务日汇率。

        当日缺失时取此前最近一个已报价日；只有反向报价时取倒数。
        锁定后不再随汇率表更新而变化——调用方应把返回值固化到业务对象上。
        """
        if base == quote:
            return Decimal("1")
        direct = self._lookup(day, base, quote)
        if direct is not None:
            return direct
        inverse = self._lookup(day, quote, base)
        if inverse is not None:
            return (Decimal("1") / inverse).quantize(Decimal("0.00000001"))
        raise FxError(f"{day} 缺少 {base}/{quote} 汇率")

    def _lookup(self, day: date, base: str, quote: str):
        candidates = [d for (d, b, q) in self._rates if b == base and q == quote and d <= day]
        if not candidates:
            return None
        return self._rates[(max(candidates), base, quote)]
