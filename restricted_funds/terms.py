"""资助限制条款。

条款维度对应 contract.json 的 restriction_dimensions：
- material_origin  材料产地（domestic / imported）
- artifact_class   器物级别（一级/二级/一般……）
- expense_category 支出类别（加固剂/工具/检测……）
- valid_period     资助有效期（闭区间 [start, end]）

操作符：
- in / not_in  字段值属于 / 不属于允许集合
- within       日期落在条款有效期内（针对业务日）
多个 Term 在同一基金内为 AND；任一不满足即禁止由该基金支付。
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Term:
    dimension: str           # material_origin / artifact_class / expense_category / valid_period
    operator: str            # in / not_in / within
    values: tuple = ()       # in/not_in 的允许值集合；within 为 (start_iso, end_iso)
    clause: str = ""         # 条款原文（被拒付款时作为依据回显）

    def violates(self, context: dict) -> str | None:
        """返回被违反的条款原文；满足时返回 None。"""
        if self.dimension == "valid_period":
            value = context.get("business_date")
            if value is None:
                return None
            if isinstance(value, str):
                value = date.fromisoformat(value)
            start, end = self.values
            if isinstance(start, str):
                start = date.fromisoformat(start)
            if isinstance(end, str):
                end = date.fromisoformat(end)
            ok = start <= value <= end
        else:
            value = context.get(self.dimension)
            if value is None:
                return None
            if self.operator == "in":
                ok = value in self.values
            elif self.operator == "not_in":
                ok = value not in self.values
            else:
                raise ValueError(f"未知操作符：{self.operator}")
        return None if ok else (self.clause or f"{self.dimension} {self.operator} {list(self.values)}")


@dataclass
class Fund:
    fund_id: str
    name: str
    currency: str
    terms: list[Term] = field(default_factory=list)

    def evaluate(self, context: dict) -> list[str]:
        """返回被违反的条款原文列表（空列表表示该基金可支付此支出）。"""
        return [v for term in self.terms if (v := term.violates(context))]
