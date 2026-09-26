"""``invoice.amount_match@1`` — the deterministic check of the invoice-payment fixture.

The fixture package (``tests/fixtures/packages/invoice-payment``) is the
second domain that proves the verification stage neutral (CP-ADR-0067 §9):
core names none of this, the package brings it as data. The amounts are
decimal strings, compared exactly.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

AMOUNT = {"type": "string", "pattern": r"^\d+(\.\d{1,2})?$"}

CONTRACT: dict[str, Any] = {
    "inputs": {
        "type": "object",
        "properties": {"invoiceAmount": AMOUNT, "paymentAmount": AMOUNT},
        "required": ["invoiceAmount", "paymentAmount"],
        "additionalProperties": False,
    },
    "outputs": {
        "type": "object",
        "properties": {"matches": {"type": "boolean"}, "difference": {"type": "string"}},
        "required": ["matches", "difference"],
    },
    "implementation": {"protocol": "local", "entrypoint": "tests.skill_stubs.amount_match:run"},
}


def run(inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        difference = Decimal(inputs["paymentAmount"]) - Decimal(inputs["invoiceAmount"])
    except InvalidOperation as exc:
        raise ValueError("an amount is not a decimal") from exc
    return {"matches": difference == 0, "difference": str(difference)}
