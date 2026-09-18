"""合规拒绝：携带条款依据，供月底逐笔解释被拒付款。"""


class ComplianceError(Exception):
    def __init__(self, code: str, message: str, bases: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # 被违反的资助条款原文依据（可有多条，条款之间为 AND）
        self.bases = bases or []

    def __str__(self):
        if self.bases:
            return f"[{self.code}] {self.message}｜条款依据：{'；'.join(self.bases)}"
        return f"[{self.code}] {self.message}"
