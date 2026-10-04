"""Monthly bank statements (PDF): only the totals.

A statement is reduced to one line per product and currency: what went out (debits for an account,
purchases for a card), what came in (credits / payments) and the opening and closing balance. Those
totals are compared with the transactions captured from alert emails (did the alerts catch everything
the card was billed for?) and tracked month to month.

Parsers are tried in order; each recognises its own bank's layout from the PDF text. To support another
bank, add a module with a parser class (see bac.py) and list it in PARSERS.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

KINDS = ("account", "card", "assets", "liabilities")


@dataclass
class StatementLine:
    kind: str  # "account" | "card" | "assets" (total) | "liabilities" (total)
    label: str  # e.g. "American Express ****1111" or "Account ****2233"
    currency: str
    last4: str | None = None  # account / card account number
    cards: list[str] = field(default_factory=list)  # card numbers whose purchases this line bills
    opening: float | None = None
    closing: float | None = None
    debits: float | None = None  # money out: account debits, card purchases (net of refunds)
    credits: float | None = None  # money in: account credits, card payments received
    verified: bool | None = None  # opening - debits + credits == closing, when the statement allows checking


@dataclass
class Statement:
    bank: str
    kind: str  # "account" | "card"
    month: date  # first day of the month the statement covers
    period_start: date | None
    period_end: date  # cut-off date
    lines: list[StatementLine]


class StatementError(ValueError):
    pass


class StatementParser:
    bank = ""
    kind = ""

    def detect(self, text: str) -> bool:
        raise NotImplementedError

    def parse(self, text: str) -> Statement:
        raise NotImplementedError


# ---- shared helpers for parsers ------------------------------------------------------------------

MONTHS_ES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8,
             "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12}
_AMOUNT = r"-?[\d,]*\.\d{2}-?"


def amount(text: str) -> float:
    """'1,336,296.17' -> 1336296.17; '12,737.35-' -> -12737.35; '.30' -> 0.3. Thousands separators are
    dropped wholesale, which also repairs PDF text where one went missing ('12074,067.15')."""
    t = text.strip().replace(",", "")
    negative = t.endswith("-") or t.startswith("-")
    value = float(t.strip("-"))
    return -value if negative else value


def es_date(day: str, month: str, year: str) -> date:
    y = int(year)
    return date(y + 2000 if y < 100 else y, MONTHS_ES[month.upper()[:3]], int(day))


def pdf_text(data: bytes, password: str = "") -> str:
    """Plain text of every page. Encrypted PDFs are opened with `password` (many banks use the ID number)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise StatementError("the PDF is password-protected; set FRAUDALERT_STATEMENT_PASSWORD")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise StatementError(f"not a readable PDF: {exc}") from None


def _parsers() -> list[StatementParser]:
    from fraudalert.statements import bac

    return [bac.BacCardStatement(), bac.BacAccountStatement()]


def parse_text(text: str) -> Statement:
    for parser in _parsers():
        if parser.detect(text):
            return parser.parse(text)
    raise StatementError("not a statement layout this app recognises (supported: BAC Credomatic accounts and cards)")


def parse_pdf(data: bytes, password: str = "") -> Statement:
    return parse_text(pdf_text(data, password))


def mask(number: str) -> str | None:
    digits = re.sub(r"\D", "", number or "")
    return digits[-4:] if digits else None
