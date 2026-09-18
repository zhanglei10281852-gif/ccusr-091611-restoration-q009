"""金额工具：统一 Decimal 与两位小数舍入（四舍五入）。"""

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def D(value) -> Decimal:
    """把 float/int/str 安全转为 Decimal（float 先转 str，避免二进制误差）。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q2(value) -> Decimal:
    """舍入到分。"""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def allocate_by_ratio(total: Decimal, ratios: list[Decimal]) -> list[Decimal]:
    """按比例分摊总额，差额加给最大的一份，保证各份之和恰好等于总额。"""
    ratios = [D(r) for r in ratios]
    base = sum(ratios)
    if base <= 0:
        raise ValueError("分摊基数必须为正")
    parts = [q2(total * r / base) for r in ratios]
    diff = q2(total - sum(parts))
    if diff:
        idx = max(range(len(parts)), key=lambda i: parts[i])
        parts[idx] = q2(parts[idx] + diff)
    return parts
