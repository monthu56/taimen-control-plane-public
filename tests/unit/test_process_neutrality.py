"""The process language of the core knows no domain (CP-ADR-0074 §15; constitution art. II).

The engine, the check of a definition, the CEL profile and the schema of the
kind ``Process`` are one language for every process a tenant describes — a
procurement and an invoice alike are packages on top of it. This guard reads
them and fails on a word of a domain; a new example in a docstring is written
with neutral names (``case``, ``item``, ``sample``).
"""

import re
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[2] / "src" / "control_plane" / "domain"
GUARDED = [*sorted(DOMAIN.glob("process_*")), DOMAIN / "cel_profile.py"]

# Words of the domains the platform's own packages cover and of the usual
# neighbours; whole words (or word stems in Russian), case-insensitive.
DOMAIN_WORDS = [
    # procurement and tenders
    r"tenders?",
    r"procurements?",
    r"purchases?",
    r"suppliers?",
    r"vendors?",
    r"bids?",
    r"okpd2?",
    r"zakupki",
    r"eis",
    r"44-?fz",
    r"223-?fz",
    r"закуп\w*",
    r"тендер\w*",
    r"поставщик\w*",
    r"заказчик\w*",
    r"нмцк",
    r"окпд\w*",
    r"еис",
    r"44-?фз",
    r"223-?фз",
    # invoices and payments
    r"invoices?",
    r"payments?",
    r"payables?",
    r"счёт\w*",
    r"счет\w*",
    r"оплат\w*",
    r"платёж\w*",
    r"платеж\w*",
    r"контрагент\w*",
    # other neighbours
    r"customers?",
    r"salar(y|ies)",
    r"patients?",
    r"loans?",
    r"клиент\w*",
    r"договор\w*",
]
PATTERN = re.compile(r"(?<![\w-])(" + "|".join(DOMAIN_WORDS) + r")(?![\w-])", re.IGNORECASE)

# Text copied verbatim from the superproject catalog schema, which the core copy
# must equal (test_the_core_copy_of_the_kind_schema_is_the_catalog_one): an example
# in a description, not a word of the language. Remove an entry once the catalog
# words its example neutrally.
CATALOG_EXAMPLES = {
    # $defs/memoryWhere/items/properties/attr (company-knowledge K001, recall.where).
    "process_spec.schema.json": ("например okpd2 или validUntil",),
}


def test_the_guard_reads_the_whole_language() -> None:
    names = {path.name for path in GUARDED}
    assert {
        "process_definition.py",
        "process_engine.py",
        "process_spec.schema.json",
        "cel_profile.py",
    } <= names


@pytest.mark.parametrize("path", GUARDED, ids=lambda p: p.name)
def test_no_domain_word_in_the_process_language(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for example in CATALOG_EXAMPLES.get(path.name, ()):
        assert example in text, f"{example!r} left the catalog: drop it from CATALOG_EXAMPLES"
        text = text.replace(example, "")
    found = [
        f"{path.name}:{number}: {match.group(0)!r}"
        for number, line in enumerate(text.splitlines(), start=1)
        for match in PATTERN.finditer(line)
    ]
    assert not found, "the process language names a domain:\n" + "\n".join(found)


@pytest.mark.parametrize(
    "text",
    ["a tender", "Invoice", "оплата счёта", "по 44-фз", "в закупке", "data.purchase.amount"],
)
def test_the_guard_sees_a_domain_word(text: str) -> None:
    assert PATTERN.search(text)


@pytest.mark.parametrize("text", ["thread", "the case", "sample.opened", "a bidirectional link"])
def test_the_guard_does_not_see_neutral_words(text: str) -> None:
    assert not PATTERN.search(text)
