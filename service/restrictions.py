"""资助限制表达求值：给出合规结论与条款依据。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Restriction


@dataclass
class ComplianceDecision:
    allowed: bool
    violations: list[Restriction] = field(default_factory=list)

    @property
    def clauses(self) -> list[str]:
        """被违反条款的原文依据，用于逐笔解释被拒付款。"""
        return [r.clause for r in self.violations]


def _as_set(value) -> set:
    if value is None:
        return set()
    if isinstance(value, (set, frozenset, list, tuple)):
        return set(value)
    return {value}


def _violated(restriction: Restriction, context: dict) -> bool:
    dim = restriction.dimension
    rule = restriction.rule
    if dim == "valid_period":
        day = context.get("business_date")
        if day is None:
            return False
        not_before = rule.get("not_before")
        not_after = rule.get("not_after")
        if not_before and day < date.fromisoformat(not_before):
            return True
        if not_after and day > date.fromisoformat(not_after):
            return True
        return False
    values = _as_set(context.get(dim))
    if "deny" in rule and values & set(rule["deny"]):
        return True
    if "allow" in rule and values and not values <= set(rule["allow"]):
        return True
    return False


def evaluate_restrictions(restrictions: list[Restriction], context: dict) -> ComplianceDecision:
    """context 键：material_origin / artifact_class / expense_category / business_date。

    artifact_class 可以是集合（一笔付款覆盖多件器物时逐类核对）。
    """
    violations = [r for r in restrictions if _violated(r, context)]
    return ComplianceDecision(allowed=not violations, violations=violations)
