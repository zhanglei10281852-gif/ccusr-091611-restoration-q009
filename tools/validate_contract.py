import json
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
sample = json.loads((root / "examples" / "allocations.json").read_text(encoding="utf-8"))
required = set(contract["required_allocation_fields"])
invoice = sample["invoice"]
assert invoice["currency"] in contract["currencies"]
assert all(required <= set(row) for row in sample["allocations"])
assert all(row["currency"] == invoice["currency"] for row in sample["allocations"])
allocated = sum(Decimal(str(row["amount"])) for row in sample["allocations"])
assert allocated == Decimal(str(invoice["gross_amount"]))
print("受限资金契约与分摊样例格式有效")

